"""What the nurse queue screen shows and does. No UI code here, so it is tested like everything else.

Nurses are care team members: they see the patient's name, risk, barriers and the whole thread (patient, AI and
nurse messages together). They reply as themselves, resolve tasks, and hand cases back to the AI.
"""

import hashlib

from readmit import db
from readmit.outreach import store


def queue() -> list[dict]:
    """Open tasks in the order a nurse should work them, with who the patient is and how late each one is."""
    t = store.now()
    rows = []
    for task in store.list_nurse_tasks():
        s = store.patient_summary(task["case_id"])
        due = task["due_at"]
        minutes_late = int((t - _dt(due)).total_seconds() // 60)
        rows.append(
            {
                **task,
                "patient": s["patient"]["first_name"],
                "language": s["patient"]["preferred_language"],
                "tier": (s.get("risk") or {}).get("tier"),
                "minutes_late": max(minutes_late, 0),
            }
        )
    return rows


def case_view(case_id: str) -> dict:
    """Everything a nurse needs on one screen."""
    s = store.patient_summary(case_id)
    case = store.get_case(case_id, events=10)
    return {
        "summary": s,
        "status": case["status"],
        "thread": store.get_thread(case_id, limit=50),
        "appointment": store.upcoming_appointment(case_id),
        "tasks": [t for t in store.list_nurse_tasks() if t["case_id"] == case_id],
        "events": case["recent_events"],
    }


def reply(task_id: str, case_id: str, channel: str, body: str, urgent: bool = False) -> dict:
    """A nurse writes to the patient in the same thread as the AI. Same consent and channel rules; an urgent
    callback is not held for quiet hours. The same text for the same task is never sent twice."""
    key = f"{task_id}:nurse:{hashlib.sha256(body.encode()).hexdigest()[:10]}"
    return store.send_message(case_id, channel, body, key, priority="safety" if urgent else "normal", author="nurse")


def resolve(task_id: str, note: str, hand_back: bool) -> dict:
    """Close the task. Hand back: the AI resumes (booked cases stay booked)."""
    return store.resolve_nurse_task(task_id, note, hand_back=hand_back)


def capacity() -> dict:
    """Open work against what the team can do: routine callbacks per business day."""
    with db.transaction() as c:
        rows = db.rows(
            c,
            """select priority, due_at::date as day, count(*) as n from outreach.nurse_tasks
               where status = 'open' group by priority, due_at::date order by day""",
        )
    return {
        "urgent_open": sum(r["n"] for r in rows if r["priority"] == "urgent"),
        "routine_by_day": {str(r["day"]): r["n"] for r in rows if r["priority"] == "routine"},
        "daily_capacity": store.NURSE_DAILY_CAPACITY,
    }


def _dt(s):
    from datetime import datetime

    return datetime.fromisoformat(str(s))
