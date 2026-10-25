"""One case's whole story, in order: discharge, risk, every message, every decision, nurse work, visits, outcome.

    make timeline         # http://localhost:8502

Read only. Built from the tables the system already writes (cases, events, messages, nurse tasks, appointments,
referrals): no extra logging, so what you see is exactly the audit trail. No UI code here, so it is tested.

Every item has: at, day (days since discharge), actor (patient, ai, nurse, clinic, system), kind, title, detail.
"""

from datetime import datetime

from readmit import db
from readmit.outreach import store

KINDS = ("message", "decision", "nurse", "visit", "risk", "outcome", "status")
ICONS = {
    "message": "💬",
    "decision": "🧭",
    "nurse": "🩺",
    "visit": "📅",
    "risk": "📈",
    "outcome": "🏁",
    "status": "·",
}
SKIP = {  # shown from their own tables instead (messages, nurse tasks), so not twice
    "message_sent",
    "message_received",
    "message_held",
    "nurse_task_created",
    "nurse_task_resolved",
}


def _pct(x) -> str:
    return f"{float(x) * 100:.0f}%"


def describe(type_: str, d: dict | None) -> tuple[str, str, str, str] | None:
    """One audit event in plain words: (kind, actor, title, detail), or None to leave it out."""
    d = d or {}
    if type_ in SKIP:
        return None
    if type_ == "note":
        note = str(d.get("note", ""))
        if note.startswith("intent: "):
            return "decision", "ai", f"Read the reply as: {note[8:].replace('_', ' ')}", ""
        if note.startswith("escalated"):
            return None  # the nurse task item says the same, with the reason
        return "decision", "ai", note, ""
    if type_ == "status_changed":
        reason = d.get("reason") or ""
        return "status", "system", f"Status: {d.get('from')} → {d.get('to')}", reason
    if type_ == "appointment_booked":
        how = "video visit" if d.get("mode") == "telehealth" else "clinic visit"
        return "visit", "ai", f"Booked a {how}", f"for {str(d.get('start_at'))[:16]}"
    if type_ == "appointment_rescheduled":
        return "visit", "ai", "Rescheduled the visit", f"{d.get('from_slot')} → {d.get('to_slot')}"
    if type_ == "appointment_cancelled":
        by = d.get("by")
        return "visit", "clinic" if by == "clinic" else "system", "Visit cancelled", str(d.get("reason") or "")
    if type_ == "visit_completed":
        return "visit", "clinic", "Attended the follow-up visit", ""
    if type_ == "visit_no_show":
        return "visit", "clinic", "Missed the visit (no-show)", "outreach restarts to rebook"
    if type_ == "referral_requested":
        return "decision", "ai", f"Filed a {str(d.get('type', '')).replace('_', ' ')} referral", ""
    if type_ == "barrier_recorded":
        return "decision", "ai", f"Noted a barrier: {str(d.get('barrier', '')).replace('_', ' ')}", ""
    if type_ == "memory_saved":
        return "decision", "ai", f"Remembered a preference: {d.get('key')} = {d.get('value')}", ""
    if type_ == "language_changed":
        return "decision", "ai", f"Language: {d.get('previous')} → {d.get('language')}", "the patient's choice"
    if type_ == "language_asked":
        return "decision", "ai", f"Asked once whether to write in {d.get('language')}", ""
    if type_ == "consent_changed":
        return "decision", "ai", f"Channel: {d.get('previous')} → {d.get('preferred_channel')}", str(d.get("reason"))
    if type_ == "identity_verified":
        return "decision", "ai", "Identity confirmed (year of birth)", ""
    if type_ in ("identity_failed", "identity_locked"):
        title = "Identity check failed" if type_ == "identity_failed" else "Identity check locked: nurse to call"
        return "decision", "ai", title, f"attempt {d.get('attempt')}"
    if type_ == "delivery_failed":
        return "message", "system", f"Couldn't deliver by {d.get('channel')}", str(d.get("reason") or "")
    if type_ == "rescored":
        before, after = d.get("before"), d.get("after")
        trigger = str(d.get("trigger", "")).replace("_", " ")
        when = f"day {d.get('day'):g}" if isinstance(d.get("day"), int | float) else ""
        detail = f"{trigger}, {when}" if when else trigger
        if before is None or after is None:
            return "risk", "system", "Re-scored", detail
        arrow = "↑" if after - before >= 0.05 else "↓" if before - after >= 0.05 else "→"
        return "risk", "system", f"Re-scored {_pct(before)} {arrow} {_pct(after)}", detail
    if type_ == "readmitted":
        return "outcome", "system", "Readmitted to the hospital", f"admitted {str(d.get('admitted_at'))[:16]}"
    return "status", "system", type_.replace("_", " "), ""


def _day(at, discharged) -> float | None:
    if not discharged:
        return None
    return round((_dt(at) - _dt(discharged)).total_seconds() / 86400, 2)


def _dt(s) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(str(s))


def build(case_id: str) -> dict:
    """{'header': who and why, 'items': the story oldest first, 'risk': [(day, risk)], 'counts': per kind}."""
    s = store.patient_summary(case_id)
    with db.transaction() as c:
        case = db.one(c, "select * from outreach.cases where case_id = :id", id=case_id)
        stay = db.one(c, "select discharge_datetime from cohort.stays where encounter_id = :e", e=case["encounter_id"])
        events = db.rows(
            c, "select at, type, detail from outreach.events where case_id = :id order by event_id", id=case_id
        )
        messages = db.rows(c, "select * from outreach.messages where case_id = :id order by seq", id=case_id)
        tasks = db.rows(
            c, "select * from outreach.nurse_tasks where case_id = :id order by created_at, task_id", id=case_id
        )
    discharged = stay["discharge_datetime"] if stay else None
    risk0 = (s.get("risk") or {}).get("risk")
    items = []

    def add(at, kind, actor, title, detail="", **extra):
        items.append(
            {"at": str(at)[:16], "day": _day(at, discharged), "kind": kind, "actor": actor, "title": title,
             "detail": detail, **extra}
        )  # fmt: skip

    if discharged:
        add(discharged, "outcome", "system", f"Discharged: {(s.get('stay') or {}).get('diagnosis_group')}",
            (s.get("stay") or {}).get("disposition") or "")  # fmt: skip
    if risk0 is not None:
        tier = (s.get("risk") or {}).get("tier")
        add(case["opened_at"], "risk", "system", f"Flagged {tier} risk ({_pct(risk0)}): outreach case opened",
            ", ".join(_driver(x) for x in (s["risk"].get("top_drivers") or [])))  # fmt: skip
    for m in messages:
        author = m.get("author") or ("patient" if m["direction"] == "inbound" else "ai")
        held = m.get("deliver_at")
        add(
            m["created_at"],
            "message",
            author,
            {"patient": "Patient wrote", "ai": "AI sent", "nurse": "Nurse sent"}.get(author, author),
            f"held for quiet hours until {str(held)[:16]}" if held else "",
            body=m["body"],
            subject=m.get("subject"),
            channel=m["channel"],
            status=m["status"],
        )
    for t in tasks:
        extra = f" · interpreter: {t['interpreter']}" if t.get("interpreter") else ""
        add(t["created_at"], "nurse", "ai", f"Handed to a nurse: {t['kind']} ({t['priority']}){extra}",
            t["reason"], priority=t["priority"])  # fmt: skip
        if t.get("resolved_at"):
            add(t["resolved_at"], "nurse", "nurse", "Nurse resolved the task", t.get("resolution") or "")
    for e in events:
        if said := describe(e["type"], e["detail"]):
            add(e["at"], *said)
    if case.get("closed_at") and case["status"] != "readmitted":
        add(case["closed_at"], "outcome", "system", f"Case closed: {case['status']}", case.get("close_reason") or "")
    items.sort(key=lambda i: (i["at"], _rank(i)))  # stable: same-minute items keep table order

    risk_path = [(0.0, float(risk0))] if risk0 is not None else []
    for e in events:
        d = e["detail"] or {}
        if e["type"] == "rescored" and d.get("after") is not None:
            risk_path.append((float(d.get("day") or _day(e["at"], discharged) or 0), float(d["after"])))
    return {
        "header": {
            "case_id": case_id,
            "first_name": s["patient"]["first_name"],
            "age": s["patient"]["age"],
            "language": s["patient"]["preferred_language"],
            "language_source": s["patient"]["language_source"],
            "diagnosis": (s.get("stay") or {}).get("diagnosis_group"),
            "discharged": (s.get("stay") or {}).get("discharged"),
            "tier": (s.get("risk") or {}).get("tier"),
            "risk_at_discharge": risk0,
            "risk_now": case["risk"],
            "drivers": [_driver(x) for x in ((s.get("risk") or {}).get("top_drivers") or [])],
            "barriers": (s.get("risk") or {}).get("barriers") or [],
            "channels": s["contact"]["allowed_channels"],
            "telehealth_ok": s["contact"]["telehealth_ok"],
            "own_doctor": s["care_team"]["own_doctor"],
            "follow_up_by": s["care_team"]["follow_up_by"],
            "status": case["status"],
            "attempts": case["attempts"],
        },
        "items": items,
        "risk": risk_path,
        "counts": {k: sum(i["kind"] == k for i in items) for k in KINDS},
    }


def _rank(item: dict) -> int:
    """Order inside one turn, where everything shares the simulation clock's minute: what the patient wrote, then
    what the AI decided and did, then the AI's message, then the status it left the case in."""
    if item["kind"] == "message":
        return 1 if item["actor"] == "patient" else 6
    if item["kind"] == "outcome":
        return 0 if item["title"].startswith("Discharged") else 8
    return {"risk": 2, "decision": 3, "visit": 4, "nurse": 5, "status": 7}.get(item["kind"], 9)


def _driver(x) -> str:
    """A model driver for people: {'feature': 'prior_admits_12m', ...} -> 'prior admits 12m'."""
    return (x.get("feature") if isinstance(x, dict) else str(x)).replace("_", " ")


# ------------------------------------------------------------------ finding a case worth looking at
INTERESTING = [  # (label, SQL returning case_id ordered by how much story there is)
    ("Readmitted", "select case_id from outreach.events where type = 'readmitted'"),
    ("Urgent nurse handoff", "select case_id from outreach.nurse_tasks where priority = 'urgent'"),
    ("Needed an interpreter", "select case_id from outreach.nurse_tasks where interpreter is not null"),
    ("Switched language", "select case_id from outreach.events where type = 'language_changed'"),
    ("Missed a visit", "select case_id from outreach.events where type = 'visit_no_show'"),
    ("Attended the visit", "select case_id from outreach.events where type = 'visit_completed'"),
    ("Risk rose after discharge",
     ("select case_id from outreach.events where type = 'rescored' "
      "and (detail->>'after')::float - (detail->>'before')::float >= 0.05")),
    ("Filed a referral", "select case_id from outreach.referrals"),
    ("Opted out", "select case_id from outreach.cases where status = 'opted_out'"),
    ("No reply, closed", "select case_id from outreach.cases where status = 'closed' and close_reason not like '%declin%'"),
]  # fmt: skip


def interesting() -> list[dict]:
    """One example case per story worth showing (the one with the most messages), for the shortcut buttons."""
    out, seen = [], set()
    with db.transaction() as c:
        for label, sql in INTERESTING:
            row = db.one(
                c,
                f"""select x.case_id, count(m.message_id) as n from ({sql}) x
                    left join outreach.messages m using (case_id)
                    group by x.case_id order by (x.case_id = any(:seen)), n desc, x.case_id limit 1""",
                seen=list(seen) or [""],
            )
            if row:
                out.append({"label": label, "case_id": row["case_id"], "messages": row["n"]})
                seen.add(row["case_id"])
    return out


def cases(limit: int = 500) -> list[dict]:
    """Cases for the picker, the busiest first."""
    with db.transaction() as c:
        return db.rows(
            c,
            """select c.case_id, p.first_name, c.tier, c.status, c.risk, count(m.message_id) as messages
               from outreach.cases c join sim.patients p using (patient_id)
               left join outreach.messages m using (case_id)
               group by c.case_id, p.first_name, c.tier, c.status, c.risk
               order by messages desc, c.case_id limit :n""",
            n=limit,
        )
