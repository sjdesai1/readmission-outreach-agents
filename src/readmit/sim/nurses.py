"""Simulated nurses: they work the queue so the simulation doesn't stall on escalated cases.

On call any hour for emergencies and urgent symptoms; routine work on weekdays 8 AM to 5 PM, a few tasks per
hour, in queue order (the same order the nurse screen shows). Each call is noted in the thread as a nurse
message. Most cases are handed back to the AI; abusive messages and patients we can't message close.
"""

from readmit.nurse import console
from readmit.outreach import store

SHIFT = (8, 17)
ROUTINE_PER_HOUR = 3
NOTE = {
    "English": "This is Nurse Kim from your care team. Thanks for talking with me today. "
    "We'll keep helping you with your follow-up visit.",
    "Spanish": "Habla la enfermera Kim de su equipo de atención. Gracias por hablar conmigo hoy. "
    "Seguiremos ayudándole con su visita de seguimiento.",
}


def work(t) -> int:
    """Handle what the nurses would handle this hour. Returns how many tasks were resolved."""
    on_shift = t.weekday() < 5 and SHIFT[0] <= t.hour < SHIFT[1]
    done = routine = 0
    for task in store.list_nurse_tasks():
        urgent = task["priority"] == "urgent"
        if not urgent and (not on_shift or routine >= ROUTINE_PER_HOUR):
            continue
        handle(task)
        done += 1
        routine += not urgent
    return done


def handle(task: dict) -> None:
    s = store.patient_summary(task["case_id"])
    allowed = s["contact"]["allowed_channels"]
    if allowed and task["kind"] != "review":
        lang = s["patient"]["preferred_language"]
        channel = s["contact"]["preferred_channel"] if s["contact"]["preferred_channel"] in allowed else allowed[0]
        try:
            console.reply(
                task["task_id"],
                task["case_id"],
                channel,
                NOTE.get(lang, NOTE["English"]),
                urgent=task["priority"] == "urgent",
            )
        except store.StoreError:
            pass  # the call happened; the note just couldn't be sent
    abusive_or_unreachable = task["kind"] == "review" and (
        "abusive" in task["reason"]
        or "no channel" in task["reason"]
        or "handle it directly" in task["reason"]  # the AI keeps failing: the nurse takes the case over
        or not allowed
    )
    if abusive_or_unreachable:
        console.resolve(task["task_id"], "nurse called; continuing by phone, outreach closed", hand_back=False)
        try:
            store.update_case(task["case_id"], "closed", "nurse handling directly")
        except store.StoreError:
            pass
    else:
        console.resolve(task["task_id"], f"nurse called about: {task['reason'][:120]}", hand_back=True)
