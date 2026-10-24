"""What the nurse queue screen shows and does. No UI code here, so it is tested like everything else.

Nurses are care team members: they see the patient's name, risk, barriers and the whole thread (patient, AI and
nurse messages together). They reply as themselves, resolve tasks, and hand cases back to the AI.

Messages not in English get a machine translation under the original, labeled as such: enough to triage, not to
act on clinically. A task that needs an interpreter says which language.
"""

import hashlib

from pydantic import BaseModel

from readmit import db
from readmit.agents import language
from readmit.outreach import store

TRANSLATE_PROMPT = "v1"


class Translation(BaseModel):
    language: str
    english: str


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


def needs_translation(body: str | None) -> bool:
    """Worth translating: written in a language other than English (short replies can't be told, so they're shown
    as they are)."""
    wrote = language.detect(body)
    return wrote is not None and wrote != "English"


def translate(body: str) -> dict | None:
    """Machine translation for the nurse: {'language', 'english'}, or None when it isn't needed or the LLM is
    unavailable (over budget, no key). Cached like every LLM call, so redrawing the screen is free."""
    if not needs_translation(body):
        return None
    from readmit import llm

    try:
        t, _ = llm.complete_json(
            task="translate",
            prompt_version=TRANSLATE_PROMPT,
            system=llm.load_prompt(f"translate_{TRANSLATE_PROMPT}"),
            user=body,
            schema=Translation,
        )
    except llm.LLMError:
        return None
    return {"language": t.language, "english": t.english}


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
