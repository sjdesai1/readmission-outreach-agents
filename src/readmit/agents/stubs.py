"""Placeholder agents: fixed rules, no LLM. They let the whole graph run and be tested before any tokens
are spent. Steps 2-4 replace them one by one with real agents that have the same signature:

    async def agent(state: TurnState, gw: Gateway) -> dict   # returns only the state keys it changes
"""

import re

from readmit.agents.gateway import Gateway, ToolFailed
from readmit.agents.guards import search_window, slot_fits
from readmit.agents.state import TurnState, request_details, when


async def care_planner(state: TurnState, gw: Gateway) -> dict:
    contact = state["summary"]["contact"]
    allowed, preferred = contact["allowed_channels"], contact["preferred_channel"]
    channel = preferred if preferred in allowed else (allowed[0] if allowed else None)
    return {
        "plan": {
            "goal": "follow-up visit within 7 days of discharge",
            "channel": channel,
            "talking_points": [],
            "referrals": [],
        }
    }


STUB_INTENTS = [  # keyword rules, first match wins: enough for free simulation runs, not for production
    ("symptom", r"\b(swollen|swelling|hinchad\w*|dizzy|mareado|fever|fiebre|short of breath)\b"),
    ("clinical_question", r"\b(can i take|medication|medicine|pills|dose|pastillas|medicina)\b"),
    ("barrier", r"\b(ride|transport\w*|no car|can'?t get there)\b"),
    ("wants_human", r"\b(nurse|real person|call me|enfermera)\b"),
    ("abusive", r"\b(useless|idiots?|stupid)\b"),
]


async def reply_interpreter(state: TurnState, gw: Gateway) -> dict:
    t = (state.get("inbound") or "").lower()
    for name, pattern in STUB_INTENTS:
        if re.search(pattern, t):
            return {"intent": {"intent": name, "accepts": False, "details": {}}}
    if re.search(r"\b(yes|book|sure|ok|okay|first|works|sí|si|primera|primero)\b", t):
        intent = {"intent": "wants_visit", "accepts": True, "details": {}}
    elif re.search(r"\b(no thanks|not interested|don'?t need)\b", t):
        intent = {"intent": "decline", "accepts": False, "details": {}}
    elif "?" in t:
        intent = {"intent": "question", "accepts": False, "details": {}}
    else:
        intent = {"intent": "unclear", "accepts": False, "details": {}}
    return {"intent": intent}


async def scheduler(state: TurnState, gw: Gateway) -> dict:
    case_id, intent = state["case_id"], state.get("intent") or {}
    details = request_details(state)  # this reply on top of standing preferences
    note = None
    try:
        earliest, latest = search_window(details, state["now"])
        found = (
            await gw.call(
                "scheduling",
                "find_slots",
                case_id=case_id,
                earliest=earliest,
                latest=latest,
                part_of_day=details.get("part_of_day"),
                limit=10,
            )
            or []
        )
        fitting = [s for s in found if not slot_fits(s["start_at"], details)][:3]
        if not fitting:  # nothing matches the request: offer the closest options and say so
            fitting = await gw.call("scheduling", "find_slots", case_id=case_id, limit=3) or []
            note = "no open times match what the patient asked for; these are the closest options"
        offered = [
            {
                "slot_id": s["slot_id"],
                "start_at": s["start_at"],
                "when": when(s["start_at"]),
                "mode": s["mode"],
                "provider": s["provider"],
                "own_doctor": s.get("own_doctor", False),
            }
            for s in fitting
        ]
        appointment = None
        if intent.get("accepts") and offered and not note and not slot_fits(offered[0]["start_at"], details):
            appointment = await gw.call(
                "scheduling",
                "book_appointment",
                case_id=case_id,
                slot_id=offered[0]["slot_id"],
                idempotency_key=f"{state['turn_key']}:book",
            )
            appointment["when"] = when(appointment["start_at"])
            appointment["provider"] = offered[0]["provider"]
    except ToolFailed as e:
        return {"schedule": {"offered": [], "appointment": None, "error": e.message}}
    return {"schedule": {"offered": offered, "appointment": appointment, "error": None, "note": note}}


def how(mode: str) -> str:
    """One label per visit type, used by every writer: the patient must know whether to travel."""
    return "video visit" if mode == "telehealth" else "clinic visit"


async def message_writer(state: TurnState, gw: Gateway) -> dict:
    name = state["summary"]["patient"]["first_name"]
    sched = state.get("schedule") or {}
    intent = (state.get("intent") or {}).get("intent")
    if sched.get("appointment"):
        a = sched["appointment"]
        body = f"Hi {name}, your follow-up visit is booked for {a['when']} ({how(a['mode'])})."
    elif sched.get("offered"):
        options = "; ".join(f"{i}) {s['when']} ({how(s['mode'])})" for i, s in enumerate(sched["offered"], 1))
        body = (
            f"Hi {name}, this is your care team at Orchard Valley. We'd like to see you for a follow-up visit "
            f"after your hospital stay. Open times: {options}. Reply with the number that works for you."
        )
    elif intent == "channel_change":
        ok = (state.get("channel_change") or {}).get("ok")
        body = (
            f"Thanks {name}, we'll use this channel from now on."
            if ok
            else f"Thanks {name}. We can't switch to that channel yet; a nurse will help update your contact details."
        )
    elif intent == "barrier":
        body = (
            f"Thanks for telling us, {name}. We can help: a video visit from home is an option, and our team can "
            "arrange a ride. Would you like us to find a time?"
        )
    elif intent == "decline":
        body = f"Thanks for letting us know, {name}. If you change your mind, just reply to this message."
    elif intent == "question":
        body = f"Thanks for your question, {name}. A nurse from your care team will follow up with you."
    else:
        body = (
            f"Sorry {name}, we didn't quite understand. Would you like us to book a follow-up visit? Reply YES or NO."
        )
    return {"draft": {"subject": "Your follow-up visit", "body": body}}


async def safety_reviewer(state: TurnState, gw: Gateway) -> dict:
    return {"review": {"verdict": "approve", "reasons": []}}


STUBS = {
    "care_planner": care_planner,
    "reply_interpreter": reply_interpreter,
    "scheduler": scheduler,
    "message_writer": message_writer,
    "safety_reviewer": safety_reviewer,
}
