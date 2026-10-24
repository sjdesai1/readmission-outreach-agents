"""Business rules for outreach. Every agent action ends up here, through an MCP server.

Rules enforced here, not in prompts (an agent can't talk its way around them):
  * minimum necessary: agents get a patient summary, never phone numbers or email addresses
  * booking: slot must exist, be open and in the future; one active appointment per patient;
    telehealth only with telehealth consent and a capable device; atomic (no double booking)
  * messaging: only channels the patient opted into; nothing to opted-out or closed cases; no outreach during
    quiet hours (held until 8 AM), except safety messages
  * scheduling: the patient's own doctor, a provider who speaks their language, and the follow-up deadline
    for their diagnosis rank first
  * consent: a channel change is recorded only if the contact detail exists, with an audit event
  * identity: checked against the record server-side (the caller never sees the true value); locks after 3 misses
  * nurse handoff: every task gets an honest due time from nurse capacity; the case pauses until a nurse hands back
  * memory: only standing preferences, from a fixed list of keys
  * language: a patient's stated language overrides the record, only for languages the outreach supports; anything
    else goes to a nurse with an interpreter (ADR 0010)
  * idempotency: retrying a booking, message or referral with the same key returns the original
  * audit: every action writes an event on the case
"""

import json
import re
import smtplib
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage

from readmit import db
from readmit.config import settings

CHANNELS = ("portal", "sms", "email", "phone")
MODES = ("in_person", "telehealth")
REFERRAL_TYPES = ("pharmacist", "transport", "social_work", "interpreter")
CASE_STATUSES = (
    "new",
    "suppressed",
    "planned",
    "contacted",
    "awaiting_reply",
    "booking",
    "booked",
    "reminded",
    "completed",
    "no_show",
    "escalated",
    "opted_out",
    "readmitted",
    "closed",
)
CLOSED_STATUSES = ("suppressed", "completed", "opted_out", "readmitted", "closed")
MIN_LEAD = timedelta(hours=2)  # can't book a slot starting in less than 2 hours
OFFER_LEAD = timedelta(hours=18)  # offered times start tomorrow or later: patients often reply hours after
QUIET_HOURS = (20, 8)  # no outreach from 8 PM to 8 AM (patient's local time)
FAST_FOLLOW_UP = {"Heart failure": 7, "COPD exacerbation": 7, "Acute MI": 7, "Sepsis": 7}  # days; others 14
PRIORITIES = ("normal", "safety")
MAX_IDENTITY_ATTEMPTS = 3
NURSE_KINDS = ("emergency", "symptom", "clinical_question", "callback", "review")
REVIEW_REPEAT_LIMIT = 2  # after this many review tasks on one case, the next one says: handle it directly
NURSE_DAILY_CAPACITY = 20  # routine callbacks per business day (2 nurses x ~10)
URGENT_WITHIN = timedelta(hours=1)
MEMORY_KEYS = ("part_of_day", "earliest_time", "latest_time", "mode", "preferred_days")
LANGUAGES = ("English", "Spanish")  # what the AI writes in (evaluated); same list as agents/language.SUPPORTED
LANGUAGE_KEYS = ("language", "language_asked")  # kept in patient_memory, shown as the language, not as preferences
CONSENT_SQL = {
    ch: f"update sim.patients set {ch}_opt_in = true, preferred_channel = :ch where patient_id = :p" for ch in CHANNELS
}  # fixed statements, one per known channel: no user text ever reaches the SQL
SENDER = "care-team@orchardvalley.example"


class StoreError(ValueError):
    """A rule was broken. The message is written for the agent: say what's wrong and what to do instead."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ------------------------------------------------------------------ clock
def now(c=None) -> datetime:
    if c is None:
        with db.transaction() as c2:
            return now(c2)
    row = db.one(c, "select now from outreach.sim_clock where id = 1")
    if not row:
        raise StoreError("simulation clock not initialised: run `python -m readmit.outreach.setup`")
    return row["now"]


def set_now(t: datetime) -> None:
    with db.transaction() as c:
        db.rows(
            c,
            """insert into outreach.sim_clock (id, now) values (1, :t)
                      on conflict (id) do update set now = excluded.now""",
            t=t,
        )


def log_event(c, case_id: str, type_: str, detail: dict | None = None) -> None:
    db.rows(
        c,
        """insert into outreach.events (case_id, at, type, detail)
                  values (:case_id, :at, :type, cast(:detail as jsonb))""",
        case_id=case_id,
        at=now(c),
        type=type_,
        detail=json.dumps(detail or {}, default=str),
    )


def _case(c, case_id: str, lock: bool = False, acting: bool = False) -> dict:
    """Load a case. acting=True (every write) also refuses a case that hasn't opened yet on the simulation clock:
    the patient may still be in hospital, so nobody may book, message or refer for them."""
    row = db.one(c, "select * from outreach.cases where case_id = :id" + (" for update" if lock else ""), id=case_id)
    if not row:
        raise StoreError(f"case {case_id} not found")
    if acting and row["opened_at"] > now(c):
        raise StoreError(
            f"case {case_id} is not open yet (opens {row['opened_at']:%Y-%m-%d %H:%M}); no actions until then"
        )
    return row


def _patient(c, patient_id: str) -> dict:
    row = db.one(c, "select * from sim.patients where patient_id = :id", id=patient_id)
    if not row:
        raise StoreError(f"patient {patient_id} not found")
    return row


# ------------------------------------------------------------------ patient data (read only)
def patient_summary(case_id: str) -> dict:
    """What an agent needs to plan outreach, and nothing more (minimum necessary)."""
    with db.transaction() as c:
        case = _case(c, case_id)
        p = _patient(c, case["patient_id"])
        stay = db.one(
            c,
            """select encounter_id, primary_dx_group, discharge_datetime, discharge_disposition,
                                   followup_scheduled_7d, age
                            from cohort.stays where encounter_id = :e""",
            e=case["encounter_id"],
        )
        score = db.one(
            c,
            "select risk, tier, top_drivers, barriers from scoring.scores where encounter_id = :e",
            e=case["encounter_id"],
        )
        pcp = db.one(c, "select name from outreach.providers where provider_id = :p", p=case.get("pcp_provider_id"))
        memory = db.rows(c, "select key, value from outreach.patient_memory where patient_id = :p", p=p["patient_id"])
        bad = _bad_channels(c, p["patient_id"])
        told = db.rows(c, "select barrier from outreach.case_barriers where case_id = :id order by at", id=case_id)
    remembered = {m["key"]: json.loads(m["value"]) for m in memory}
    stated = {k: remembered.get(k) for k in LANGUAGE_KEYS}
    channels = {
        "portal": bool(p["has_portal_account"] and p["portal_opt_in"]),
        "sms": bool(p["sms_opt_in"] and p["mobile_phone"]),
        "email": bool(p["email_opt_in"] and p["email"]),
        "phone": bool(p["phone_opt_in"] and p["mobile_phone"]),
    }
    return {
        "case_id": case_id,
        "case_status": case["status"],
        "patient": {
            "first_name": p["first_name"],
            "age": stay["age"] if stay else None,
            "preferred_language": stated.get("language") or p["preferred_language"],
            "language_source": "patient" if stated.get("language") else "record",
            "language_asked": bool(stated.get("language_asked")),
        },
        "contact": {
            "allowed_channels": [ch for ch, ok in channels.items() if ok and ch not in bad],
            "undeliverable": sorted(bad),
            "preferred_channel": p["preferred_channel"],
            "contact_window": p["contact_window"],
            "telehealth_ok": bool(p["telehealth_opt_in"] and p["telehealth_capable"]),
        },
        "stay": None
        if not stay
        else {
            "diagnosis_group": stay["primary_dx_group"],
            "discharged": str(stay["discharge_datetime"])[:16],
            "disposition": stay["discharge_disposition"],
            "followup_already_booked": bool(stay["followup_scheduled_7d"]),
        },
        "memory": {k: v for k, v in remembered.items() if k not in LANGUAGE_KEYS},
        "care_team": {
            "own_doctor": pcp["name"] if pcp else None,
            "follow_up_by": str(case["follow_up_by"])[:10] if case.get("follow_up_by") else None,
        },
        "risk": None
        if not score
        else {
            "risk": score["risk"],
            "tier": score["tier"],
            "top_drivers": json.loads(score["top_drivers"]),
            "barriers": sorted({*json.loads(score["barriers"]), *(t["barrier"] for t in told)}),
        },
    }


# ------------------------------------------------------------------ scheduling
def find_slots(
    case_id: str,
    earliest: str | None = None,
    latest: str | None = None,
    mode: str | None = None,
    part_of_day: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """Open slots the patient can actually use. Telehealth only offered if the patient can do it."""
    if mode is not None and mode not in MODES:
        raise StoreError(f"mode must be one of {MODES}")
    if part_of_day not in (None, "morning", "afternoon"):
        raise StoreError("part_of_day must be 'morning' or 'afternoon'")
    limit = max(1, min(int(limit), 10))
    with db.transaction() as c:
        t = now(c)
        summary_ok = db.one(
            c,
            """select p.telehealth_opt_in and p.telehealth_capable as tele_ok,
                                         coalesce(m.value::json #>> '{}', p.preferred_language) as lang,
                                         k.pcp_provider_id, k.follow_up_by
                                  from outreach.cases k join sim.patients p using (patient_id)
                                  left join outreach.patient_memory m
                                    on m.patient_id = k.patient_id and m.key = 'language'
                                  where k.case_id = :id""",
            id=case_id,
        )
        if summary_ok is None:
            raise StoreError(f"case {case_id} not found")
        if mode == "telehealth" and not summary_ok["tele_ok"]:
            raise StoreError("this patient has not consented to telehealth or has no video device; offer in-person")
        start = max(t + OFFER_LEAD, datetime.fromisoformat(earliest)) if earliest else t + OFFER_LEAD
        end = datetime.fromisoformat(latest) if latest else start + timedelta(days=14)
        hours = {"morning": (0, 12), "afternoon": (12, 24)}.get(part_of_day, (0, 24))
        modes = [mode] if mode else (["in_person", "telehealth"] if summary_ok["tele_ok"] else ["in_person"])
        found = db.rows(
            c,
            """select s.slot_id, s.start_at, s.mode, s.provider_id, p.name as provider, p.role,
                                     :lang = any(p.languages) as speaks_language
                              from outreach.slots s join outreach.providers p using (provider_id)
                              where s.status = 'open' and s.start_at >= :start and s.start_at < :end
                                and extract(hour from s.start_at) >= :h0 and extract(hour from s.start_at) < :h1
                                and s.mode = any(:modes)
                              order by s.start_at limit 200""",
            start=start,
            end=end,
            h0=hours[0],
            h1=hours[1],
            modes=modes,
            lang=summary_ok["lang"],
        )
    due = summary_ok["follow_up_by"]
    for r in found:
        r["own_doctor"] = r["provider_id"] == summary_ok["pcp_provider_id"]
        r["within_target"] = due is None or r["start_at"].date() <= due.date()
    # meet the follow-up deadline first, then the patient's language, then their own doctor, then soonest
    found.sort(key=lambda r: (not r["within_target"], not r["speaks_language"], not r["own_doctor"], r["start_at"]))
    return [
        {k: v for k, v in r.items() if k != "provider_id"} | {"start_at": r["start_at"].isoformat(timespec="minutes")}
        for r in found[:limit]
    ]


def record_offers(case_id: str, slot_ids: list[str], message_id: str | None = None) -> dict:
    """The options just sent to the patient, numbered as they saw them. Replaces any earlier offer."""
    if not slot_ids:
        raise StoreError("no slots to record")
    with db.transaction() as c:
        _case(c, case_id)
        t = now(c)
        db.rows(c, "delete from outreach.offers where case_id = :id", id=case_id)
        for n, sid in enumerate(slot_ids, 1):
            db.rows(
                c,
                """insert into outreach.offers (case_id, option, slot_id, offered_at, message_id)
                   values (:id, :n, :s, :t, :m)""",
                id=case_id,
                n=n,
                s=sid,
                t=t,
                m=message_id,
            )
    return {"case_id": case_id, "options": len(slot_ids)}


def offered_slots(case_id: str) -> list[dict]:
    """What the patient was last offered, in their numbering, and whether each slot can still be booked."""
    with db.transaction() as c:
        t = now(c)
        rows = db.rows(
            c,
            """select o.option, o.slot_id, s.start_at, s.mode, s.status, p.name as provider
               from outreach.offers o join outreach.slots s using (slot_id)
               join outreach.providers p on p.provider_id = s.provider_id
               where o.case_id = :id order by o.option""",
            id=case_id,
        )
    return [
        {
            "option": r["option"],
            "slot_id": r["slot_id"],
            "start_at": r["start_at"].isoformat(timespec="minutes"),
            "mode": r["mode"],
            "provider": r["provider"],
            "still_open": r["status"] == "open" and r["start_at"] >= t + MIN_LEAD,
        }
        for r in rows
    ]


def book_appointment(case_id: str, slot_id: str, idempotency_key: str) -> dict:
    with db.transaction() as c:
        existing = db.one(c, "select * from outreach.appointments where idempotency_key = :k", k=idempotency_key)
        if existing:
            return {**_appt_view(existing), "note": "already booked by an earlier identical request"}
        case = _case(c, case_id, lock=True, acting=True)
        if case["status"] in CLOSED_STATUSES:
            raise StoreError(f"case is {case['status']}; no booking allowed")
        pid = case["patient_id"]
        active = db.one(
            c,
            """select appointment_id, start_at from outreach.appointments
                              where patient_id = :p and status = 'booked' and start_at > :t""",
            p=pid,
            t=now(c),
        )
        if active:
            raise StoreError(
                f"patient already has appointment {active['appointment_id']} at {active['start_at']}; "
                "use reschedule_appointment instead"
            )
        slot = db.one(c, "select * from outreach.slots where slot_id = :s for update", s=slot_id)
        if not slot:
            raise StoreError(f"slot {slot_id} does not exist; call find_slots")
        if slot["status"] != "open":
            raise StoreError(f"slot {slot_id} is no longer available; call find_slots for current options")
        if slot["start_at"] < now(c) + MIN_LEAD:
            raise StoreError(f"slot {slot_id} is in the past or too soon; call find_slots")
        if slot["mode"] == "telehealth":
            p = _patient(c, pid)
            if not (p["telehealth_opt_in"] and p["telehealth_capable"]):
                raise StoreError("patient cannot do telehealth; choose an in-person slot")
        appt_id = _id("appt")
        t = now(c)
        link = f"https://telehealth.orchardvalley.example/v/{appt_id}" if slot["mode"] == "telehealth" else None
        db.rows(
            c,
            """update outreach.slots set status = 'booked', patient_id = :p, appointment_id = :a
                      where slot_id = :s""",
            p=pid,
            a=appt_id,
            s=slot_id,
        )
        appt = db.one(
            c,
            """insert into outreach.appointments (appointment_id, idempotency_key, case_id, patient_id,
                                slot_id, provider_id, start_at, mode, status, video_link, created_at, updated_at)
                            values (:a, :k, :case_id, :p, :s, :prov, :start, :mode, 'booked', :link, :t, :t)
                            returning *""",
            a=appt_id,
            k=idempotency_key,
            case_id=case_id,
            p=pid,
            s=slot_id,
            prov=slot["provider_id"],
            start=slot["start_at"],
            mode=slot["mode"],
            link=link,
            t=t,
        )
        db.rows(
            c,
            """update outreach.cases set status = 'booked', rebook_reason = null, updated_at = :t
               where case_id = :id""",
            t=t,
            id=case_id,
        )
        log_event(
            c,
            case_id,
            "appointment_booked",
            {"appointment_id": appt_id, "slot_id": slot_id, "start_at": slot["start_at"], "mode": slot["mode"]},
        )
    return _appt_view(appt)


def reschedule_appointment(appointment_id: str, new_slot_id: str, idempotency_key: str) -> dict:
    with db.transaction() as c:
        dup = db.one(
            c,
            "select * from outreach.events where type = 'appointment_rescheduled' and detail->>'idempotency_key' = :k",
            k=idempotency_key,
        )
        appt = db.one(c, "select * from outreach.appointments where appointment_id = :a for update", a=appointment_id)
        if not appt:
            raise StoreError(f"appointment {appointment_id} not found")
        if dup:
            return {**_appt_view(appt), "note": "already rescheduled by an earlier identical request"}
        if appt["status"] != "booked":
            raise StoreError(f"appointment is {appt['status']}; book a new one instead")
        new = db.one(c, "select * from outreach.slots where slot_id = :s for update", s=new_slot_id)
        if not new or new["status"] != "open" or new["start_at"] < now(c) + MIN_LEAD:
            raise StoreError(f"slot {new_slot_id} is not available; call find_slots")
        if new["mode"] == "telehealth":
            p = _patient(c, appt["patient_id"])
            if not (p["telehealth_opt_in"] and p["telehealth_capable"]):
                raise StoreError("patient cannot do telehealth; choose an in-person slot")
        t, old_slot = now(c), appt["slot_id"]
        db.rows(
            c,
            "update outreach.slots set status = 'open', patient_id = null, appointment_id = null where slot_id = :s",
            s=old_slot,
        )
        db.rows(
            c,
            "update outreach.slots set status = 'booked', patient_id = :p, appointment_id = :a where slot_id = :s",
            p=appt["patient_id"],
            a=appointment_id,
            s=new_slot_id,
        )
        link = f"https://telehealth.orchardvalley.example/v/{appointment_id}" if new["mode"] == "telehealth" else None
        appt = db.one(
            c,
            """update outreach.appointments set slot_id = :s, provider_id = :prov, start_at = :start,
                                   mode = :mode, video_link = :link, updated_at = :t
                            where appointment_id = :a returning *""",
            s=new_slot_id,
            prov=new["provider_id"],
            start=new["start_at"],
            mode=new["mode"],
            link=link,
            t=t,
            a=appointment_id,
        )
        log_event(
            c,
            appt["case_id"],
            "appointment_rescheduled",
            {
                "appointment_id": appointment_id,
                "from_slot": old_slot,
                "to_slot": new_slot_id,
                "idempotency_key": idempotency_key,
            },
        )
    return _appt_view(appt)


def cancel_appointment(appointment_id: str, reason: str) -> dict:
    with db.transaction() as c:
        appt = db.one(c, "select * from outreach.appointments where appointment_id = :a for update", a=appointment_id)
        if not appt:
            raise StoreError(f"appointment {appointment_id} not found")
        if appt["status"] == "cancelled":
            return {**_appt_view(appt), "note": "already cancelled"}
        t = now(c)
        db.rows(
            c,
            "update outreach.slots set status = 'open', patient_id = null, appointment_id = null where slot_id = :s",
            s=appt["slot_id"],
        )
        appt = db.one(
            c,
            "update outreach.appointments set status = 'cancelled', updated_at = :t "
            "where appointment_id = :a returning *",
            t=t,
            a=appointment_id,
        )
        log_event(c, appt["case_id"], "appointment_cancelled", {"appointment_id": appointment_id, "reason": reason})
    return _appt_view(appt)


# ------------------------------------------------------------------ delivery failures, barriers, readmission
SHARED_NUMBER = {"sms": ("sms", "phone"), "phone": ("sms", "phone"), "email": ("email",), "portal": ("portal",)}
BARRIER_WORDS = {  # word starts (regex) in what a patient said -> the barrier name the model already uses
    "transport_barrier": ("ride", r"car\b", "drive", r"bus\b", "transport"),
    "cost_barrier": ("afford", "cost", "copay", "money", "insurance"),
    "caregiving_barrier": ("childcare", "kids", "babysitter", "care for my"),
    "work_schedule": ("work", "shift", "job", "trabajo"),
}


def _bad_channels(c, patient_id: str) -> dict[str, str]:
    rows = db.rows(c, "select channel, reason from outreach.bad_contacts where patient_id = :p", p=patient_id)
    return {r["channel"]: r["reason"] for r in rows}


def delivery_failed(message_id: str, reason: str) -> dict:
    """The carrier or mail server reported a failure (bounce, wrong number). The channel is marked undeliverable
    for this patient (a wrong number breaks texts and calls alike) and the case is due again right away, so the
    next attempt goes out on another channel."""
    with db.transaction() as c:
        m = db.one(c, "select * from outreach.messages where message_id = :m for update", m=message_id)
        if not m:
            raise StoreError(f"message {message_id} not found")
        t = now(c)
        db.rows(c, "update outreach.messages set status = 'failed' where message_id = :m", m=message_id)
        for ch in SHARED_NUMBER[m["channel"]]:
            db.rows(
                c,
                """insert into outreach.bad_contacts (patient_id, channel, reason, at) values (:p, :ch, :r, :t)
                   on conflict (patient_id, channel) do nothing""",
                p=m["patient_id"],
                ch=ch,
                r=reason,
                t=t,
            )
        db.rows(
            c,
            """update outreach.cases set next_action_at = :t, updated_at = :t
               where case_id = :id and status = 'awaiting_reply'""",
            t=t,
            id=m["case_id"],
        )
        log_event(
            c, m["case_id"], "delivery_failed", {"message_id": message_id, "channel": m["channel"], "reason": reason}
        )
    return {"message_id": message_id, "status": "failed", "reason": reason}


def barrier_name(text: str) -> str:
    t = text.lower()
    hit = (name for name, words in BARRIER_WORDS.items() if any(re.search(rf"\b{w}", t) for w in words))
    return next(hit, "other_barrier")


def record_barrier(case_id: str, detail: str, source: str) -> dict:
    """A barrier the patient told us about ("no ride"), named the way the risk model names barriers, so the
    planner sees it next to the ones extracted from the hospital notes."""
    name = barrier_name(detail)
    with db.transaction() as c:
        _case(c, case_id, acting=True)
        db.rows(
            c,
            """insert into outreach.case_barriers (case_id, barrier, detail, source, at)
               values (:id, :b, :d, :s, :t) on conflict (case_id, barrier) do nothing""",
            id=case_id,
            b=name,
            d=detail[:300],
            s=source,
            t=now(c),
        )
        log_event(c, case_id, "barrier_recorded", {"barrier": name, "source": source})
    return {"barrier": name}


def mark_readmitted(case_id: str, admitted_at) -> dict:
    """The patient is back in the hospital. Outreach stops at once and upcoming visits are cancelled; a new case
    opens after the new discharge. A case already closed keeps its status: the event is still logged, because
    readmission after outreach is what the program is measured on."""
    with db.transaction() as c:
        case = _case(c, case_id, lock=True)
        t = now(c)
        for a in db.rows(
            c,
            "select appointment_id, slot_id from outreach.appointments where case_id = :id and status = 'booked'",
            id=case_id,
        ):
            db.rows(
                c,
                "update outreach.slots set status = 'open', patient_id = null, appointment_id = null where slot_id = :s",
                s=a["slot_id"],
            )
            db.rows(
                c,
                "update outreach.appointments set status = 'cancelled', updated_at = :t where appointment_id = :a",
                t=t,
                a=a["appointment_id"],
            )
        if case["status"] not in CLOSED_STATUSES:
            db.rows(
                c,
                """update outreach.cases set status = 'readmitted', closed_at = :t, close_reason = 'readmitted',
                          next_action_at = null, updated_at = :t where case_id = :id""",
                t=t,
                id=case_id,
            )
        log_event(c, case_id, "readmitted", {"admitted_at": str(admitted_at), "status_before": case["status"]})
    return get_case(case_id, events=3)


RESCORE_TRIGGERS = ("landmark", "ed_visit", "missed_fill")
NO_RESCORE = ("readmitted", "suppressed")  # back in the hospital (also logged for closed cases), or never started


def rescore(case_id: str, risk: float, day: float, trigger: str = "landmark") -> dict:
    """A re-score after discharge: on a landmark day (7, 14, 21) or right after an event (an ED visit, no
    prescription fill by day 3). The case's risk is updated, so outreach and queues reorder. Cases whose outreach
    ended (visit completed, closed) are still re-scored: the risk keeps mattering until day 30. `open` says
    whether outreach is still running."""
    if trigger not in RESCORE_TRIGGERS:
        raise StoreError(f"trigger must be one of {RESCORE_TRIGGERS}")
    with db.transaction() as c:
        case = _case(c, case_id, lock=True)
        readmitted = db.one(
            c, "select count(*) as n from outreach.events where case_id = :id and type = 'readmitted'", id=case_id
        )["n"]
        if case["status"] in NO_RESCORE or readmitted:
            return {"case_id": case_id, "skipped": "readmitted" if readmitted else f"case is {case['status']}"}
        db.rows(
            c, "update outreach.cases set risk = :r, updated_at = :t where case_id = :id", r=risk, t=now(c), id=case_id
        )
        log_event(
            c,
            case_id,
            "rescored",
            {
                "trigger": trigger,
                "day": round(float(day), 2),
                "before": case["risk"],
                "after": risk,
                "open": case["status"] not in CLOSED_STATUSES,
            },
        )
    return {
        "case_id": case_id,
        "trigger": trigger,
        "day": day,
        "before": case["risk"],
        "after": risk,
        "open": case["status"] not in CLOSED_STATUSES,
        "status": case["status"],
    }


def rescore_summary() -> list[dict]:
    """Re-scores by trigger: how many, how many on open cases, how many raised the risk 5+ points, the average
    risk before and after, and the nurse callbacks they created."""
    with db.transaction() as c:
        return db.rows(
            c,
            """with r as (
                   select detail->>'trigger' as trigger, (detail->>'before')::float as before,
                          (detail->>'after')::float as after, coalesce((detail->>'open')::boolean, true) as open
                   from outreach.events where type = 'rescored'),
               a as (
                   select split_part(idempotency_key, ':', 3) as trigger, count(*) as alerts
                   from outreach.nurse_tasks where idempotency_key like '%:rescore:%' group by 1)
               select r.trigger, count(*) as n, count(*) filter (where r.open) as open,
                      count(*) filter (where r.after - r.before >= 0.05) as rose,
                      avg(r.before) as before, avg(r.after) as after, coalesce(max(a.alerts), 0) as alerts
               from r left join a using (trigger)
               group by r.trigger order by r.trigger""",
        )


# ------------------------------------------------------------------ appointment lifecycle
REMIND_WITHIN = timedelta(hours=26)  # remind the day before
REBOOK_REASONS = ("no_show", "clinic_cancelled")


def upcoming_appointment(case_id: str) -> dict | None:
    """The case's next booked visit, with the doctor's name (for reminders)."""
    with db.transaction() as c:
        a = db.one(
            c,
            """select a.*, p.name as provider from outreach.appointments a
               join outreach.providers p using (provider_id)
               where a.case_id = :id and a.status = 'booked' and a.start_at > :t order by a.start_at limit 1""",
            id=case_id,
            t=now(c),
        )
    return {**_appt_view(a), "provider": a["provider"]} if a else None


def reminders_due() -> list[dict]:
    """Booked visits starting within 26h (but not within 2h) that haven't had a reminder. Visits booked in the
    last 12h are skipped: the confirmation was the reminder. Used by the sequencer, not agents."""
    with db.transaction() as c:
        t = now(c)
        return db.rows(
            c,
            """select a.case_id, a.appointment_id from outreach.appointments a
               join outreach.cases k using (case_id)
               where a.status = 'booked' and a.start_at > :soon and a.start_at <= :horizon
                 and a.created_at <= :settled and k.status = 'booked'
                 and not exists (select 1 from outreach.messages m
                                 where m.idempotency_key = a.appointment_id || :suffix)
               order by a.start_at""",
            soon=t + MIN_LEAD,
            horizon=t + REMIND_WITHIN,
            settled=t - timedelta(hours=12),
            suffix=":reminder",  # passed as a value: ":name" inside SQL text is read as a parameter
        )


def visits_to_settle() -> list[dict]:
    """Booked visits that ended over an hour ago: the clinic records whether the patient came."""
    with db.transaction() as c:
        return db.rows(
            c,
            """select appointment_id, case_id, patient_id, start_at from outreach.appointments
               where status = 'booked' and start_at + interval '1 hour' <= :t order by start_at, case_id""",
            t=now(c),
        )


def _needs_rebooking(c, case_id: str, reason: str, t) -> None:
    db.rows(
        c,
        """update outreach.cases set status = :s, rebook_reason = :r, rebook_at = :t, next_action_at = :t,
                  updated_at = :t
           where case_id = :id and status <> all(:closed)""",
        s="no_show" if reason == "no_show" else "awaiting_reply",
        r=reason,
        t=t,
        id=case_id,
        closed=list(CLOSED_STATUSES),
    )


def record_visit(appointment_id: str, attended: bool) -> dict:
    """The clinic marks a visit attended (case completed) or missed (outreach restarts to rebook)."""
    with db.transaction() as c:
        a = db.one(c, "select * from outreach.appointments where appointment_id = :a for update", a=appointment_id)
        if not a:
            raise StoreError(f"appointment {appointment_id} not found")
        if a["status"] != "booked":
            return {**_appt_view(a), "note": f"already {a['status']}"}
        t = now(c)
        a = db.one(
            c,
            """update outreach.appointments set status = :s, updated_at = :t where appointment_id = :a
               returning *""",
            s="completed" if attended else "no_show",
            t=t,
            a=appointment_id,
        )
        if attended:
            db.rows(
                c,
                """update outreach.cases set status = 'completed', closed_at = :t, close_reason = 'visit attended',
                          next_action_at = null, updated_at = :t
                   where case_id = :id and status <> all(:closed)""",
                t=t,
                id=a["case_id"],
                closed=list(CLOSED_STATUSES),
            )
        else:
            _needs_rebooking(c, a["case_id"], "no_show", t)
        log_event(
            c, a["case_id"], "visit_completed" if attended else "visit_no_show", {"appointment_id": appointment_id}
        )
    return _appt_view(a)


def clinic_cancel(appointment_id: str, reason: str) -> dict:
    """The clinic cancels a booked visit (doctor out, schedule change). The slot is blocked, not reopened, and
    the patient is offered new times."""
    with db.transaction() as c:
        a = db.one(c, "select * from outreach.appointments where appointment_id = :a for update", a=appointment_id)
        if not a or a["status"] != "booked":
            raise StoreError(f"appointment {appointment_id} is not booked")
        t = now(c)
        db.rows(
            c,
            "update outreach.slots set status = 'held_other', patient_id = null, appointment_id = null "
            "where slot_id = :s",
            s=a["slot_id"],
        )
        a = db.one(
            c,
            """update outreach.appointments set status = 'cancelled', updated_at = :t where appointment_id = :a
               returning *""",
            t=t,
            a=appointment_id,
        )
        _needs_rebooking(c, a["case_id"], "clinic_cancelled", t)
        log_event(
            c,
            a["case_id"],
            "appointment_cancelled",
            {"appointment_id": appointment_id, "by": "clinic", "reason": reason},
        )
    return _appt_view(a)


def _appt_view(a: dict) -> dict:
    return {
        "appointment_id": a["appointment_id"],
        "case_id": a["case_id"],
        "start_at": str(a["start_at"]),
        "mode": a["mode"],
        "status": a["status"],
        "video_link": a["video_link"],
        "provider_id": a["provider_id"],
    }


# ------------------------------------------------------------------ messaging
def in_quiet_hours(t: datetime) -> bool:
    return t.hour >= QUIET_HOURS[0] or t.hour < QUIET_HOURS[1]


def _next_morning(t: datetime) -> datetime:
    day = t.date() if t.hour < QUIET_HOURS[1] else t.date() + timedelta(days=1)
    return datetime.combine(day, datetime.min.time()).replace(hour=QUIET_HOURS[1])


def send_message(
    case_id: str,
    channel: str,
    body: str,
    idempotency_key: str,
    subject: str | None = None,
    priority: str = "normal",
    author: str = "ai",
) -> dict:
    """Send through one channel. Refused unless the patient opted into it. Contact details stay server-side.
    During quiet hours a normal message is held and delivered at 8 AM; safety messages go out immediately.
    author: 'ai' for the agents (the only value the MCP tool can send), 'nurse' from the nurse console."""
    if author not in ("ai", "nurse"):
        raise StoreError("author must be 'ai' or 'nurse'")
    if channel not in CHANNELS:
        raise StoreError(f"channel must be one of {CHANNELS}")
    if priority not in PRIORITIES:
        raise StoreError(f"priority must be one of {PRIORITIES}")
    if not body or not body.strip():
        raise StoreError("message body is empty")
    with db.transaction() as c:
        existing = db.one(c, "select * from outreach.messages where idempotency_key = :k", k=idempotency_key)
        if existing:
            return {**_msg_view(existing), "note": "already sent by an earlier identical request; not sent again"}
        case = _case(c, case_id, acting=True)
        if case["status"] in CLOSED_STATUSES:
            raise StoreError(f"case is {case['status']}; no messages may be sent")
        p = _patient(c, case["patient_id"])
        allowed = {
            "portal": p["has_portal_account"] and p["portal_opt_in"],
            "sms": p["sms_opt_in"] and p["mobile_phone"],
            "email": p["email_opt_in"] and p["email"],
            "phone": p["phone_opt_in"] and p["mobile_phone"],
        }
        bad = _bad_channels(c, p["patient_id"])
        if channel in bad:
            raise StoreError(
                f"messages by {channel} are not getting through ({bad[channel]}); use another consented channel"
            )
        if not allowed[channel]:
            raise StoreError(
                f"patient has not opted into {channel}; use one of "
                f"{[ch for ch, ok in allowed.items() if ok] or 'no channel (escalate to nurse)'}"
            )
        t = now(c)
        hold = priority == "normal" and in_quiet_hours(t)
        status = (
            "scheduled" if hold else {"portal": "delivered", "sms": "sent", "email": "sent", "phone": "queued"}[channel]
        )
        # record first, then send, inside one transaction: if sending fails the record rolls back too
        msg = db.one(
            c,
            """insert into outreach.messages (message_id, idempotency_key, case_id, patient_id, direction,
                               channel, subject, body, status, created_at, deliver_at, author)
                           values (:m, :k, :case_id, :p, 'outbound', :ch, :subj, :body, :status, :t, :d, :a)
                           returning *""",
            a=author,
            m=_id("msg"),
            k=idempotency_key,
            case_id=case_id,
            p=p["patient_id"],
            ch=channel,
            subj=subject,
            body=body,
            status=status,
            t=t,
            d=_next_morning(t) if hold else None,
        )
        if hold:
            log_event(c, case_id, "message_held", {"message_id": msg["message_id"], "until": str(msg["deliver_at"])})
            return _msg_view(msg)
        if channel == "email":
            _send_email(p["email"], subject or "A message from your care team", body)
        db.rows(
            c,
            "update outreach.cases set attempts = attempts + 1, updated_at = :t where case_id = :id",
            t=now(c),
            id=case_id,
        )
        log_event(c, case_id, "message_sent", {"message_id": msg["message_id"], "channel": channel})
    return _msg_view(msg)


def record_inbound(case_id: str, channel: str, body: str) -> dict:
    """A patient reply arriving (used by the simulator / webhook, not by agents)."""
    with db.transaction() as c:
        case = _case(c, case_id)
        msg = db.one(
            c,
            """insert into outreach.messages (message_id, case_id, patient_id, direction, channel, body,
                               status, created_at, author)
                           values (:m, :case_id, :p, 'inbound', :ch, :body, 'received', :t, 'patient') returning *""",
            m=_id("msg"),
            case_id=case_id,
            p=case["patient_id"],
            ch=channel,
            body=body,
            t=now(c),
        )
        log_event(c, case_id, "message_received", {"message_id": msg["message_id"], "channel": channel})
    return _msg_view(msg)


def get_thread(case_id: str, limit: int = 20) -> list[dict]:
    with db.transaction() as c:
        _case(c, case_id)
        msgs = db.rows(
            c,
            """select * from (select * from outreach.messages where case_id = :id
                                            order by seq desc limit :lim) m order by seq""",
            id=case_id,
            lim=max(1, min(int(limit), 50)),
        )
    return [_msg_view(m) for m in msgs]


def _msg_view(m: dict) -> dict:
    view = {
        "message_id": m["message_id"],
        "direction": m["direction"],
        "channel": m["channel"],
        "subject": m["subject"],
        "body": m["body"],
        "status": m["status"],
        "at": str(m["created_at"]),
        "author": m.get("author") or ("patient" if m["direction"] == "inbound" else "ai"),
    }
    if m.get("deliver_at"):
        view["deliver_at"] = str(m["deliver_at"])
    return view


def deliver_due() -> int:
    """Send messages held for quiet hours whose time has come (run by the sequencer each simulated hour)."""
    sent = 0
    with db.transaction() as c:
        t = now(c)
        held = db.rows(
            c,
            """select m.*, p.email from outreach.messages m join sim.patients p using (patient_id)
                             where m.status = 'scheduled' and m.deliver_at <= :t for update of m""",
            t=t,
        )
        for m in held:
            if m["channel"] == "email":
                _send_email(m["email"], m["subject"] or "A message from your care team", m["body"])
            status = {"portal": "delivered", "sms": "sent", "email": "sent", "phone": "queued"}[m["channel"]]
            db.rows(c, "update outreach.messages set status = :s where message_id = :m", s=status, m=m["message_id"])
            log_event(
                c,
                m["case_id"],
                "message_sent",
                {"message_id": m["message_id"], "channel": m["channel"], "after_hold": True},
            )
            sent += 1
    return sent


def _send_email(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SENDER, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
        s.send_message(msg)


# ------------------------------------------------------------------ referrals
def create_referral(case_id: str, type_: str, reason: str, idempotency_key: str) -> dict:
    if type_ not in REFERRAL_TYPES:
        raise StoreError(f"referral type must be one of {REFERRAL_TYPES}")
    with db.transaction() as c:
        existing = db.one(c, "select * from outreach.referrals where idempotency_key = :k", k=idempotency_key)
        if existing:
            return {**_ref_view(existing), "note": "already requested by an earlier identical request"}
        case = _case(c, case_id, acting=True)
        ref = db.one(
            c,
            """insert into outreach.referrals (referral_id, idempotency_key, case_id, patient_id, type,
                               reason, status, created_at)
                           values (:r, :k, :case_id, :p, :type, :reason, 'requested', :t) returning *""",
            r=_id("ref"),
            k=idempotency_key,
            case_id=case_id,
            p=case["patient_id"],
            type=type_,
            reason=reason,
            t=now(c),
        )
        log_event(c, case_id, "referral_requested", {"referral_id": ref["referral_id"], "type": type_})
    return _ref_view(ref)


def _ref_view(r: dict) -> dict:
    return {"referral_id": r["referral_id"], "type": r["type"], "reason": r["reason"], "status": r["status"]}


# ------------------------------------------------------------------ cases
def get_case(case_id: str, events: int = 15) -> dict:
    with db.transaction() as c:
        case = _case(c, case_id)
        ev = db.rows(
            c,
            """select * from (select event_id, at, type, detail from outreach.events where case_id = :id
                                          order by event_id desc limit :n) e order by event_id""",
            id=case_id,
            n=events,
        )
    return {
        **{k: (str(v) if isinstance(v, datetime) else v) for k, v in case.items()},
        "recent_events": [{"at": str(e["at"]), "type": e["type"], "detail": e["detail"]} for e in ev],
    }


def update_case(case_id: str, status: str, reason: str, next_action_at: str | None = None) -> dict:
    if status not in CASE_STATUSES:
        raise StoreError(f"status must be one of {CASE_STATUSES}")
    with db.transaction() as c:
        case = _case(c, case_id, lock=True, acting=True)
        if case["status"] in CLOSED_STATUSES:
            raise StoreError(f"case is already {case['status']}")
        t = now(c)
        closed = status in CLOSED_STATUSES
        db.rows(
            c,
            """update outreach.cases set status = :s, next_action_at = :n, updated_at = :t,
                             closed_at = case when :closed then :t else closed_at end,
                             close_reason = case when :closed then :reason else close_reason end
                      where case_id = :id""",
            s=status,
            n=datetime.fromisoformat(next_action_at) if next_action_at else None,
            t=t,
            closed=closed,
            reason=reason,
            id=case_id,
        )
        log_event(c, case_id, "status_changed", {"from": case["status"], "to": status, "reason": reason})
    return get_case(case_id, events=5)


def set_preferred_channel(case_id: str, channel: str, reason: str) -> dict:
    """The patient asked to be contacted another way. Recorded only if we hold that contact detail."""
    if channel not in CHANNELS:
        raise StoreError(f"channel must be one of {CHANNELS}")
    have = {"portal": "has_portal_account", "sms": "mobile_phone", "phone": "mobile_phone", "email": "email"}
    with db.transaction() as c:
        case = _case(c, case_id, acting=True)
        p = _patient(c, case["patient_id"])
        if not p[have[channel]]:
            raise StoreError(
                f"no {channel} contact on file for this patient; a nurse must collect it first. "
                "Keep using the current channel."
            )
        db.rows(c, CONSENT_SQL[channel], ch=channel, p=p["patient_id"])
        log_event(
            c,
            case_id,
            "consent_changed",
            {
                "preferred_channel": channel,
                "previous": p["preferred_channel"],
                "reason": reason,
                "source": "patient message",
            },
        )
    return patient_summary(case_id)["contact"]


def verify_identity(case_id: str, birth_year: int) -> dict:
    """Compare the year of birth someone gave with the record. Returns only yes/no and attempts left."""
    with db.transaction() as c:
        case = _case(c, case_id, lock=True, acting=True)
        if case["identity_verified_at"]:
            return {"verified": True, "note": "already verified for this case"}
        if case["identity_failures"] >= MAX_IDENTITY_ATTEMPTS:
            raise StoreError(
                f"identity check locked after {MAX_IDENTITY_ATTEMPTS} failed attempts; a nurse must verify the patient"
            )
        ok = int(birth_year) == int(_patient(c, case["patient_id"])["birth_year"])
        t = now(c)
        if ok:
            db.rows(c, "update outreach.cases set identity_verified_at = :t where case_id = :id", t=t, id=case_id)
            log_event(c, case_id, "identity_verified", {"method": "birth_year"})
            return {"verified": True}
        failures = case["identity_failures"] + 1
        db.rows(c, "update outreach.cases set identity_failures = :f where case_id = :id", f=failures, id=case_id)
        log_event(
            c,
            case_id,
            "identity_failed" if failures < MAX_IDENTITY_ATTEMPTS else "identity_locked",
            {"attempt": failures},
        )
        return {"verified": False, "attempts_left": MAX_IDENTITY_ATTEMPTS - failures}


# ------------------------------------------------------------------ nurse handoff
def _next_business_day(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def create_nurse_task(
    case_id: str,
    kind: str,
    priority: str,
    reason: str,
    idempotency_key: str,
    summary: str | None = None,
    interpreter: str | None = None,
) -> dict:
    """Hand a case to a nurse. The due time is honest: urgent within the hour; routine by 5 PM on the first
    business day with capacity left. The case is paused (escalated) until a nurse hands it back.
    interpreter: the language the nurse needs an interpreter for (the patient wrote in or asked for it)."""
    if kind not in NURSE_KINDS:
        raise StoreError(f"kind must be one of {NURSE_KINDS}")
    if priority not in ("urgent", "routine"):
        raise StoreError("priority must be 'urgent' or 'routine'")
    with db.transaction() as c:
        existing = db.one(c, "select * from outreach.nurse_tasks where idempotency_key = :k", k=idempotency_key)
        if existing:
            return {**_task_view(existing), "note": "already created by an earlier identical request"}
        case = _case(c, case_id, lock=True, acting=True)
        t = now(c)
        if kind == "review":  # circuit breaker: the AI keeps failing on this case, so say it plainly
            earlier = db.one(
                c, "select count(*) as n from outreach.nurse_tasks where case_id = :id and kind = 'review'", id=case_id
            )["n"]
            if earlier >= REVIEW_REPEAT_LIMIT:
                reason = f"{reason} [repeat {earlier + 1}: the AI keeps failing on this case; handle it directly]"
        if kind == "emergency":
            due = t  # now: the patient was already told to call 911
        elif priority == "urgent":
            due = t + URGENT_WITHIN
        else:
            day = _next_business_day(t.date())
            while (
                db.one(
                    c,
                    """select count(*) as n from outreach.nurse_tasks
                       where status = 'open' and priority = 'routine' and due_at::date = :d""",
                    d=day,
                )["n"]
                >= NURSE_DAILY_CAPACITY
            ):
                day = _next_business_day(day)
            due = datetime.combine(day, datetime.min.time()).replace(hour=17)
        task = db.one(
            c,
            """insert into outreach.nurse_tasks (task_id, idempotency_key, case_id, patient_id, kind, priority,
                   reason, summary, due_at, created_at, interpreter)
               values (:id, :k, :case_id, :p, :kind, :prio, :reason, :summary, :due, :t, :interp) returning *""",
            id=_id("task"),
            k=idempotency_key,
            case_id=case_id,
            p=case["patient_id"],
            kind=kind,
            prio=priority,
            reason=reason,
            summary=summary,
            due=due,
            t=t,
            interp=interpreter,
        )
        if case["status"] not in (*CLOSED_STATUSES, "escalated"):
            db.rows(
                c,
                """update outreach.cases set status = 'escalated', next_action_at = null, updated_at = :t
                   where case_id = :id""",
                t=t,
                id=case_id,
            )
            log_event(c, case_id, "status_changed", {"from": case["status"], "to": "escalated", "reason": reason})
        log_event(
            c,
            case_id,
            "nurse_task_created",
            {"task_id": task["task_id"], "kind": kind, "priority": priority, "interpreter": interpreter},
        )
    return _task_view(task)


def _task_view(t: dict) -> dict:
    return {
        "task_id": t["task_id"],
        "case_id": t["case_id"],
        "kind": t["kind"],
        "priority": t["priority"],
        "reason": t["reason"],
        "status": t["status"],
        "due_at": str(t["due_at"]),
        "interpreter": t.get("interpreter"),
    }


def list_nurse_tasks(status: str = "open") -> list[dict]:
    """The nurse queue: emergencies, then urgent, then by due time (for the nurse queue screen, not agents)."""
    with db.transaction() as c:
        rows = db.rows(
            c,
            """select * from outreach.nurse_tasks where status = :s
               order by (kind = 'emergency') desc, (priority = 'urgent') desc, due_at""",
            s=status,
        )
    return [_task_view(r) for r in rows]


def resolve_nurse_task(task_id: str, resolution: str, hand_back: bool = False) -> dict:
    """A nurse finished the task. hand_back=True returns the case to the AI outreach: back to 'booked' if the
    patient still has an upcoming appointment (so outreach doesn't offer visits again), else 'awaiting_reply'."""
    with db.transaction() as c:
        task = db.one(c, "select * from outreach.nurse_tasks where task_id = :id for update", id=task_id)
        if not task:
            raise StoreError(f"nurse task {task_id} not found")
        t = now(c)
        task = db.one(
            c,
            """update outreach.nurse_tasks set status = 'done', resolved_at = :t, resolution = :r
               where task_id = :id returning *""",
            t=t,
            r=resolution,
            id=task_id,
        )
        back_to = None
        if hand_back:
            upcoming = db.one(
                c,
                """select 1 as x from outreach.appointments
                   where case_id = :id and status = 'booked' and start_at > :t""",
                id=task["case_id"],
                t=t,
            )
            back_to = "booked" if upcoming else "awaiting_reply"
            db.rows(  # the outreach cadence restarts from the hand-back (rebook_at marks where it counts from)
                c,
                """update outreach.cases set status = :s, updated_at = :t, rebook_at = :t,
                          next_action_at = case when :s = 'booked' then null else :t end
                   where case_id = :id and status = 'escalated'""",
                s=back_to,
                t=t,
                id=task["case_id"],
            )
        log_event(
            c, task["case_id"], "nurse_task_resolved", {"task_id": task_id, "hand_back": hand_back, "to": back_to}
        )
    return _task_view(task)


# ------------------------------------------------------------------ patient memory
def remember_preference(case_id: str, key: str, value, source: str) -> dict:
    """Save a standing preference the patient stated. Only known keys; the latest statement wins."""
    if key not in MEMORY_KEYS:
        raise StoreError(f"key must be one of {MEMORY_KEYS}")
    with db.transaction() as c:
        case = _case(c, case_id, acting=True)
        _save_memory(c, case["patient_id"], key, value, source)
        log_event(c, case_id, "memory_saved", {"key": key, "value": value})
    return {"saved": key}


def _save_memory(c, patient_id: str, key: str, value, source: str) -> None:
    db.rows(
        c,
        """insert into outreach.patient_memory (patient_id, key, value, source, updated_at)
           values (:p, :k, :v, :s, :t)
           on conflict (patient_id, key) do update set value = excluded.value, source = excluded.source,
                                                     updated_at = excluded.updated_at""",
        p=patient_id,
        k=key,
        v=json.dumps(value),
        s=source[:200],
        t=now(c),
    )


def set_language(case_id: str, language: str, source: str) -> dict:
    """The patient asked for another language. Only languages the outreach is evaluated in; anything else needs a
    nurse with an interpreter. Patient-level, so it carries over to later stays; audited on the case."""
    if language not in LANGUAGES:
        raise StoreError(
            f"the outreach can't write in {language}; supported: {', '.join(LANGUAGES)}. "
            "Hand the case to a nurse with an interpreter."
        )
    with db.transaction() as c:
        case = _case(c, case_id, acting=True)
        before = db.one(
            c,
            "select value from outreach.patient_memory where patient_id = :p and key = 'language'",
            p=case["patient_id"],
        )
        previous = json.loads(before["value"]) if before else _patient(c, case["patient_id"])["preferred_language"]
        _save_memory(c, case["patient_id"], "language", language, source)
        log_event(
            c, case_id, "language_changed", {"language": language, "previous": previous, "source": "patient message"}
        )
    return {"language": language, "previous": previous}


def note_language_question(case_id: str, language: str) -> dict:
    """We asked the patient whether to switch to `language`. Asked once per patient, never again."""
    with db.transaction() as c:
        case = _case(c, case_id)
        _save_memory(c, case["patient_id"], "language_asked", language, "asked after a reply in that language")
        log_event(c, case_id, "language_asked", {"language": language})
    return {"asked": language}


def add_note(case_id: str, note: str) -> dict:
    with db.transaction() as c:
        _case(c, case_id)
        log_event(c, case_id, "note", {"note": note})
    return {"ok": True}


def due_cases(limit: int = 500) -> list[dict]:
    """Open cases whose next outreach is due at the simulated now (used by the sequencer, not by agents).
    Booked cases (reminders come later) and cases with a nurse are not outreach-due. At most one AI message a day
    starts from outreach: a case already messaged today (a reply, a reminder, a rebook) waits until tomorrow.
    A message that bounced doesn't count: it never reached the patient, so another channel can be tried today."""
    with db.transaction() as c:
        return db.rows(
            c,
            """select k.case_id, k.patient_id, k.status, k.next_action_at from outreach.cases k
                             where k.status <> all(:closed) and k.opened_at <= :t
                               and (k.next_action_at is null or k.next_action_at <= :t)
                               and not exists (
                                   select 1 from outreach.messages m
                                   where m.case_id = k.case_id and m.direction = 'outbound' and m.author = 'ai'
                                     and m.status <> 'failed'
                                     and m.created_at >= date_trunc('day', cast(:t as timestamp)))
                             order by k.risk desc limit :lim""",
            closed=[*CLOSED_STATUSES, "booked", "escalated"],
            t=now(c),
            lim=limit,
        )
