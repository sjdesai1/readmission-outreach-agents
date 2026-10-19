"""Placeholder agents: fixed rules, no LLM. They let the whole graph run and be tested before any tokens
are spent. Steps 2-4 replace them one by one with real agents that have the same signature:

    async def agent(state: TurnState, gw: Gateway) -> dict   # returns only the state keys it changes
"""

import re

from readmit.agents.gateway import Gateway, ToolFailed
from readmit.agents.guards import search_window, slot_fits
from readmit.agents.state import TurnState, request_details, when, when_es


async def care_planner(state: TurnState, gw: Gateway) -> dict:
    contact = state["summary"]["contact"]
    allowed, preferred = contact["allowed_channels"], contact["preferred_channel"]
    channel = preferred if preferred in allowed else (allowed[0] if allowed else None)
    barriers = ((state["summary"].get("risk") or {}).get("barriers")) or []
    plan = {
        "goal": "follow-up visit within 7 days of discharge",
        "channel": channel,
        "talking_points": [],
        "referrals": [],
        "referrals_filed": [],
    }
    if "transport_barrier" in barriers:  # the one barrier with a direct fix: a ride, or a video visit
        plan["talking_points"].append("we can arrange a ride, or do a video visit from home")
        try:
            await gw.call(
                "referrals",
                "create_referral",
                case_id=state["case_id"],
                referral_type="transport",
                reason="patient has no transportation to the follow-up visit",
                idempotency_key=f"{state['case_id']}:ref:transport",  # one ride referral per case
            )
            plan["referrals_filed"].append("transport")
        except ToolFailed:
            pass
    return {"plan": plan}


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
            details = {"barrier": (state.get("inbound") or "")[:200]} if name == "barrier" else {}
            return {"intent": {"intent": name, "accepts": False, "details": details}}
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


HOW_ES = {"telehealth": "por video", "in_person": "en la clínica"}
TEXT = {  # the rule-based writer's templates; also what patients get when the LLM budget runs out
    "English": {
        "subject": "Your follow-up visit",
        "booked": "Hi {name}, your follow-up visit is booked for {when} ({how}).",
        "no_show": "Hi {name}, we missed you at your visit, and that's okay. Let's find a new time.",
        "clinic_cancelled": "Hi {name}, we're sorry: the clinic had to cancel your visit. Let's find a new time.",
        "intro": "Hi {name}, this is your care team at Orchard Valley. We'd like to see you for a follow-up visit "
        "after your hospital stay.",
        "options": "Open times: {options}. Reply with the number that works for you.",
        "channel_ok": "Thanks {name}, we'll use this channel from now on.",
        "channel_no": "Thanks {name}. We can't switch to that channel yet; a nurse will help update your contact "
        "details.",
        "barrier": "Thanks for telling us, {name}. We can help: a video visit from home is an option, and our team "
        "can arrange a ride. Would you like us to find a time?",
        "decline": "Thanks for letting us know, {name}. If you change your mind, just reply to this message.",
        "question": "Thanks for your question, {name}. A nurse from your care team will follow up with you.",
        "unclear": "Sorry {name}, we didn't quite understand. Would you like us to book a follow-up visit? "
        "Reply YES or NO.",
    },
    "Spanish": {
        "subject": "Su visita de seguimiento",
        "booked": "Hola {name}, su visita de seguimiento está reservada para el {when} ({how}).",
        "no_show": "Hola {name}, le extrañamos en su visita, no se preocupe. Busquemos una nueva hora.",
        "clinic_cancelled": "Hola {name}, lo sentimos: la clínica tuvo que cancelar su visita. Busquemos una nueva "
        "hora.",
        "intro": "Hola {name}, le escribe su equipo de Orchard Valley. Queremos verle en una visita de seguimiento "
        "tras su hospitalización.",
        "options": "Horarios: {options}. Responda con el número que le funcione.",
        "channel_ok": "Gracias {name}, usaremos este medio desde ahora.",
        "channel_no": "Gracias {name}. Todavía no podemos usar ese medio; una enfermera le ayudará a actualizar sus "
        "datos de contacto.",
        "barrier": "Gracias por contarnos, {name}. Podemos ayudar: una visita por video desde casa es una opción, y "
        "nuestro equipo puede organizar transporte. ¿Quiere que busquemos una hora?",
        "decline": "Gracias por avisarnos, {name}. Si cambia de opinión, responda a este mensaje.",
        "question": "Gracias por su pregunta, {name}. Una enfermera de su equipo le contactará.",
        "unclear": "Disculpe {name}, no entendimos bien. ¿Quiere que reservemos una visita de seguimiento? "
        "Responda sí o no.",
    },
}


async def message_writer(state: TurnState, gw: Gateway) -> dict:
    name = state["summary"]["patient"]["first_name"]
    lang = state["summary"]["patient"].get("preferred_language")
    t = TEXT.get(lang, TEXT["English"])
    spanish = t is TEXT["Spanish"]

    def show(slot):  # Spanish dates in Spanish messages; the output checks read both formats
        return (when_es(slot["start_at"]) if spanish else slot["when"]), (
            HOW_ES[slot["mode"]] if spanish else how(slot["mode"])
        )

    sched = state.get("schedule") or {}
    intent = (state.get("intent") or {}).get("intent")
    if sched.get("appointment"):
        w, h = show(sched["appointment"])
        body = t["booked"].format(name=name, when=w, how=h)
    elif sched.get("offered"):
        options = "; ".join(f"{i}) {w} ({h})" for i, (w, h) in enumerate(map(show, sched["offered"]), 1))
        opening = t.get((state.get("case") or {}).get("rebook_reason") or "intro") or t["intro"]
        body = f"{opening.format(name=name)} {t['options'].format(options=options)}"
    elif intent == "channel_change":
        ok = (state.get("channel_change") or {}).get("ok")
        body = t["channel_ok" if ok else "channel_no"].format(name=name)
    elif intent in ("barrier", "decline", "question"):
        body = t[intent].format(name=name)
    else:
        body = t["unclear"].format(name=name)
    return {"draft": {"subject": t["subject"], "body": body}}


async def safety_reviewer(state: TurnState, gw: Gateway) -> dict:
    return {"review": {"verdict": "approve", "reasons": []}}


STUBS = {
    "care_planner": care_planner,
    "reply_interpreter": reply_interpreter,
    "scheduler": scheduler,
    "message_writer": message_writer,
    "safety_reviewer": safety_reviewer,
}
