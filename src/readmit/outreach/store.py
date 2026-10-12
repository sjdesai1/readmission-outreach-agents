"""Business rules for outreach. Every agent action ends up here, through an MCP server.

Rules enforced here, not in prompts (an agent can't talk its way around them):
  * minimum necessary: agents get a patient summary, never phone numbers or email addresses
  * booking: slot must exist, be open and in the future; one active appointment per patient;
    telehealth only with telehealth consent and a capable device; atomic (no double booking)
  * messaging: only channels the patient opted into; nothing to opted-out or closed cases
  * idempotency: retrying a booking, message or referral with the same key returns the original
  * audit: every action writes an event on the case
"""
import json
import smtplib
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage

from readmit import db
from readmit.config import settings

CHANNELS = ("portal", "sms", "email", "phone")
MODES = ("in_person", "telehealth")
REFERRAL_TYPES = ("pharmacist", "transport", "social_work", "interpreter")
CASE_STATUSES = ("new", "suppressed", "planned", "contacted", "awaiting_reply", "booking", "booked", "reminded",
                 "completed", "no_show", "escalated", "opted_out", "readmitted", "closed")
CLOSED_STATUSES = ("suppressed", "completed", "opted_out", "readmitted", "closed")
MIN_LEAD = timedelta(hours=2)          # can't book a slot starting in less than 2 hours
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
        db.rows(c, """insert into outreach.sim_clock (id, now) values (1, :t)
                      on conflict (id) do update set now = excluded.now""", t=t)


def log_event(c, case_id: str, type_: str, detail: dict | None = None) -> None:
    db.rows(c, """insert into outreach.events (case_id, at, type, detail)
                  values (:case_id, :at, :type, cast(:detail as jsonb))""",
            case_id=case_id, at=now(c), type=type_, detail=json.dumps(detail or {}, default=str))


def _case(c, case_id: str, lock: bool = False) -> dict:
    row = db.one(c, "select * from outreach.cases where case_id = :id" + (" for update" if lock else ""), id=case_id)
    if not row:
        raise StoreError(f"case {case_id} not found")
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
        stay = db.one(c, """select encounter_id, primary_dx_group, discharge_datetime, discharge_disposition,
                                   followup_scheduled_7d, age
                            from cohort.stays where encounter_id = :e""", e=case["encounter_id"])
        score = db.one(c, "select risk, tier, top_drivers, barriers from scoring.scores where encounter_id = :e",
                       e=case["encounter_id"])
    channels = {"portal": bool(p["has_portal_account"] and p["portal_opt_in"]),
                "sms": bool(p["sms_opt_in"] and p["mobile_phone"]),
                "email": bool(p["email_opt_in"] and p["email"]),
                "phone": bool(p["phone_opt_in"] and p["mobile_phone"])}
    return {
        "case_id": case_id,
        "case_status": case["status"],
        "patient": {"first_name": p["first_name"], "age": stay["age"] if stay else None,
                    "preferred_language": p["preferred_language"]},
        "contact": {"allowed_channels": [ch for ch, ok in channels.items() if ok],
                    "preferred_channel": p["preferred_channel"], "contact_window": p["contact_window"],
                    "telehealth_ok": bool(p["telehealth_opt_in"] and p["telehealth_capable"])},
        "stay": None if not stay else {
            "diagnosis_group": stay["primary_dx_group"], "discharged": str(stay["discharge_datetime"])[:16],
            "disposition": stay["discharge_disposition"], "followup_already_booked": bool(stay["followup_scheduled_7d"])},
        "risk": None if not score else {
            "risk": score["risk"], "tier": score["tier"], "top_drivers": json.loads(score["top_drivers"]),
            "barriers": json.loads(score["barriers"])},
    }


# ------------------------------------------------------------------ scheduling
def find_slots(case_id: str, earliest: str | None = None, latest: str | None = None, mode: str | None = None,
               part_of_day: str | None = None, limit: int = 3) -> list[dict]:
    """Open slots the patient can actually use. Telehealth only offered if the patient can do it."""
    if mode is not None and mode not in MODES:
        raise StoreError(f"mode must be one of {MODES}")
    if part_of_day not in (None, "morning", "afternoon"):
        raise StoreError("part_of_day must be 'morning' or 'afternoon'")
    limit = max(1, min(int(limit), 10))
    with db.transaction() as c:
        t = now(c)
        summary_ok = db.one(c, """select p.telehealth_opt_in and p.telehealth_capable as tele_ok
                                  from outreach.cases k join sim.patients p using (patient_id)
                                  where k.case_id = :id""", id=case_id)
        if summary_ok is None:
            raise StoreError(f"case {case_id} not found")
        if mode == "telehealth" and not summary_ok["tele_ok"]:
            raise StoreError("this patient has not consented to telehealth or has no video device; offer in-person")
        start = max(t + MIN_LEAD, datetime.fromisoformat(earliest)) if earliest else t + MIN_LEAD
        end = datetime.fromisoformat(latest) if latest else start + timedelta(days=14)
        hours = {"morning": (0, 12), "afternoon": (12, 24)}.get(part_of_day, (0, 24))
        modes = [mode] if mode else (["in_person", "telehealth"] if summary_ok["tele_ok"] else ["in_person"])
        found = db.rows(c, """select s.slot_id, s.start_at, s.mode, p.name as provider, p.role
                              from outreach.slots s join outreach.providers p using (provider_id)
                              where s.status = 'open' and s.start_at >= :start and s.start_at < :end
                                and extract(hour from s.start_at) >= :h0 and extract(hour from s.start_at) < :h1
                                and s.mode = any(:modes)
                              order by s.start_at limit :lim""",
                        start=start, end=end, h0=hours[0], h1=hours[1], modes=modes, lim=limit)
    return [{**r, "start_at": r["start_at"].isoformat(timespec="minutes")} for r in found]


def book_appointment(case_id: str, slot_id: str, idempotency_key: str) -> dict:
    with db.transaction() as c:
        existing = db.one(c, "select * from outreach.appointments where idempotency_key = :k", k=idempotency_key)
        if existing:
            return {**_appt_view(existing), "note": "already booked by an earlier identical request"}
        case = _case(c, case_id, lock=True)
        if case["status"] in CLOSED_STATUSES:
            raise StoreError(f"case is {case['status']}; no booking allowed")
        pid = case["patient_id"]
        active = db.one(c, """select appointment_id, start_at from outreach.appointments
                              where patient_id = :p and status = 'booked' and start_at > :t""", p=pid, t=now(c))
        if active:
            raise StoreError(f"patient already has appointment {active['appointment_id']} at {active['start_at']}; "
                             "use reschedule_appointment instead")
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
        db.rows(c, """update outreach.slots set status = 'booked', patient_id = :p, appointment_id = :a
                      where slot_id = :s""", p=pid, a=appt_id, s=slot_id)
        appt = db.one(c, """insert into outreach.appointments (appointment_id, idempotency_key, case_id, patient_id,
                                slot_id, provider_id, start_at, mode, status, video_link, created_at, updated_at)
                            values (:a, :k, :case_id, :p, :s, :prov, :start, :mode, 'booked', :link, :t, :t)
                            returning *""",
                    a=appt_id, k=idempotency_key, case_id=case_id, p=pid, s=slot_id, prov=slot["provider_id"],
                    start=slot["start_at"], mode=slot["mode"], link=link, t=t)
        db.rows(c, "update outreach.cases set status = 'booked', updated_at = :t where case_id = :id", t=t, id=case_id)
        log_event(c, case_id, "appointment_booked", {"appointment_id": appt_id, "slot_id": slot_id,
                                                      "start_at": slot["start_at"], "mode": slot["mode"]})
    return _appt_view(appt)


def reschedule_appointment(appointment_id: str, new_slot_id: str, idempotency_key: str) -> dict:
    with db.transaction() as c:
        dup = db.one(c, "select * from outreach.events where type = 'appointment_rescheduled' "
                        "and detail->>'idempotency_key' = :k", k=idempotency_key)
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
        db.rows(c, "update outreach.slots set status = 'open', patient_id = null, appointment_id = null "
                   "where slot_id = :s", s=old_slot)
        db.rows(c, "update outreach.slots set status = 'booked', patient_id = :p, appointment_id = :a "
                   "where slot_id = :s", p=appt["patient_id"], a=appointment_id, s=new_slot_id)
        link = f"https://telehealth.orchardvalley.example/v/{appointment_id}" if new["mode"] == "telehealth" else None
        appt = db.one(c, """update outreach.appointments set slot_id = :s, provider_id = :prov, start_at = :start,
                                   mode = :mode, video_link = :link, updated_at = :t
                            where appointment_id = :a returning *""",
                      s=new_slot_id, prov=new["provider_id"], start=new["start_at"], mode=new["mode"], link=link,
                      t=t, a=appointment_id)
        log_event(c, appt["case_id"], "appointment_rescheduled",
                  {"appointment_id": appointment_id, "from_slot": old_slot, "to_slot": new_slot_id,
                   "idempotency_key": idempotency_key})
    return _appt_view(appt)


def cancel_appointment(appointment_id: str, reason: str) -> dict:
    with db.transaction() as c:
        appt = db.one(c, "select * from outreach.appointments where appointment_id = :a for update", a=appointment_id)
        if not appt:
            raise StoreError(f"appointment {appointment_id} not found")
        if appt["status"] == "cancelled":
            return {**_appt_view(appt), "note": "already cancelled"}
        t = now(c)
        db.rows(c, "update outreach.slots set status = 'open', patient_id = null, appointment_id = null "
                   "where slot_id = :s", s=appt["slot_id"])
        appt = db.one(c, "update outreach.appointments set status = 'cancelled', updated_at = :t "
                         "where appointment_id = :a returning *", t=t, a=appointment_id)
        log_event(c, appt["case_id"], "appointment_cancelled", {"appointment_id": appointment_id, "reason": reason})
    return _appt_view(appt)


def _appt_view(a: dict) -> dict:
    return {"appointment_id": a["appointment_id"], "case_id": a["case_id"], "start_at": str(a["start_at"]),
            "mode": a["mode"], "status": a["status"], "video_link": a["video_link"], "provider_id": a["provider_id"]}


# ------------------------------------------------------------------ messaging
def send_message(case_id: str, channel: str, body: str, idempotency_key: str, subject: str | None = None) -> dict:
    """Send through one channel. Refused unless the patient opted into it. Contact details stay server-side."""
    if channel not in CHANNELS:
        raise StoreError(f"channel must be one of {CHANNELS}")
    if not body or not body.strip():
        raise StoreError("message body is empty")
    with db.transaction() as c:
        existing = db.one(c, "select * from outreach.messages where idempotency_key = :k", k=idempotency_key)
        if existing:
            return {**_msg_view(existing), "note": "already sent by an earlier identical request; not sent again"}
        case = _case(c, case_id)
        if case["status"] in CLOSED_STATUSES:
            raise StoreError(f"case is {case['status']}; no messages may be sent")
        p = _patient(c, case["patient_id"])
        allowed = {"portal": p["has_portal_account"] and p["portal_opt_in"],
                   "sms": p["sms_opt_in"] and p["mobile_phone"],
                   "email": p["email_opt_in"] and p["email"],
                   "phone": p["phone_opt_in"] and p["mobile_phone"]}
        if not allowed[channel]:
            raise StoreError(f"patient has not opted into {channel}; use one of "
                             f"{[ch for ch, ok in allowed.items() if ok] or 'no channel (escalate to nurse)'}")
        status = {"portal": "delivered", "sms": "sent", "email": "sent", "phone": "queued"}[channel]
        # record first, then send, inside one transaction: if sending fails the record rolls back too
        msg = db.one(c, """insert into outreach.messages (message_id, idempotency_key, case_id, patient_id, direction,
                               channel, subject, body, status, created_at)
                           values (:m, :k, :case_id, :p, 'outbound', :ch, :subj, :body, :status, :t) returning *""",
                     m=_id("msg"), k=idempotency_key, case_id=case_id, p=p["patient_id"], ch=channel, subj=subject,
                     body=body, status=status, t=now(c))
        if channel == "email":
            _send_email(p["email"], subject or "A message from your care team", body)
        db.rows(c, "update outreach.cases set attempts = attempts + 1, updated_at = :t where case_id = :id",
                t=now(c), id=case_id)
        log_event(c, case_id, "message_sent", {"message_id": msg["message_id"], "channel": channel})
    return _msg_view(msg)


def record_inbound(case_id: str, channel: str, body: str) -> dict:
    """A patient reply arriving (used by the simulator / webhook, not by agents)."""
    with db.transaction() as c:
        case = _case(c, case_id)
        msg = db.one(c, """insert into outreach.messages (message_id, case_id, patient_id, direction, channel, body,
                               status, created_at)
                           values (:m, :case_id, :p, 'inbound', :ch, :body, 'received', :t) returning *""",
                     m=_id("msg"), case_id=case_id, p=case["patient_id"], ch=channel, body=body, t=now(c))
        log_event(c, case_id, "message_received", {"message_id": msg["message_id"], "channel": channel})
    return _msg_view(msg)


def get_thread(case_id: str, limit: int = 20) -> list[dict]:
    with db.transaction() as c:
        _case(c, case_id)
        msgs = db.rows(c, """select * from (select * from outreach.messages where case_id = :id
                                            order by seq desc limit :lim) m order by seq""",
                       id=case_id, lim=max(1, min(int(limit), 50)))
    return [_msg_view(m) for m in msgs]


def _msg_view(m: dict) -> dict:
    return {"message_id": m["message_id"], "direction": m["direction"], "channel": m["channel"],
            "subject": m["subject"], "body": m["body"], "status": m["status"], "at": str(m["created_at"])}


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
        case = _case(c, case_id)
        ref = db.one(c, """insert into outreach.referrals (referral_id, idempotency_key, case_id, patient_id, type,
                               reason, status, created_at)
                           values (:r, :k, :case_id, :p, :type, :reason, 'requested', :t) returning *""",
                     r=_id("ref"), k=idempotency_key, case_id=case_id, p=case["patient_id"], type=type_,
                     reason=reason, t=now(c))
        log_event(c, case_id, "referral_requested", {"referral_id": ref["referral_id"], "type": type_})
    return _ref_view(ref)


def _ref_view(r: dict) -> dict:
    return {"referral_id": r["referral_id"], "type": r["type"], "reason": r["reason"], "status": r["status"]}


# ------------------------------------------------------------------ cases
def get_case(case_id: str, events: int = 15) -> dict:
    with db.transaction() as c:
        case = _case(c, case_id)
        ev = db.rows(c, """select * from (select event_id, at, type, detail from outreach.events where case_id = :id
                                          order by event_id desc limit :n) e order by event_id""", id=case_id, n=events)
    return {**{k: (str(v) if isinstance(v, datetime) else v) for k, v in case.items()},
            "recent_events": [{"at": str(e["at"]), "type": e["type"], "detail": e["detail"]} for e in ev]}


def update_case(case_id: str, status: str, reason: str, next_action_at: str | None = None) -> dict:
    if status not in CASE_STATUSES:
        raise StoreError(f"status must be one of {CASE_STATUSES}")
    with db.transaction() as c:
        case = _case(c, case_id, lock=True)
        if case["status"] in CLOSED_STATUSES:
            raise StoreError(f"case is already {case['status']}")
        t = now(c)
        closed = status in CLOSED_STATUSES
        db.rows(c, """update outreach.cases set status = :s, next_action_at = :n, updated_at = :t,
                             closed_at = case when :closed then :t else closed_at end,
                             close_reason = case when :closed then :reason else close_reason end
                      where case_id = :id""",
                s=status, n=datetime.fromisoformat(next_action_at) if next_action_at else None, t=t,
                closed=closed, reason=reason, id=case_id)
        log_event(c, case_id, "status_changed", {"from": case["status"], "to": status, "reason": reason})
    return get_case(case_id, events=5)


def add_note(case_id: str, note: str) -> dict:
    with db.transaction() as c:
        _case(c, case_id)
        log_event(c, case_id, "note", {"note": note})
    return {"ok": True}


def due_cases(limit: int = 500) -> list[dict]:
    """Open cases whose next action is due at the simulated now (used by the sequencer, not by agents)."""
    with db.transaction() as c:
        return db.rows(c, """select case_id, patient_id, status, next_action_at from outreach.cases
                             where status <> all(:closed) and opened_at <= :t
                               and (next_action_at is null or next_action_at <= :t)
                             order by risk desc limit :lim""", closed=list(CLOSED_STATUSES), t=now(c), lim=limit)
