"""Placeholder agents: fixed rules, no LLM. They let the whole graph run and be tested before any tokens
are spent. Steps 2-4 replace them one by one with real agents that have the same signature:

    async def agent(state: TurnState, gw: Gateway) -> dict   # returns only the state keys it changes
"""

import re

from readmit.agents.gateway import Gateway, ToolFailed
from readmit.agents.guards import search_window, slot_fits
from readmit.agents.state import (
    TurnState,
    on_file,
    picked_option,
    request_details,
    standing_details,
    when,
    when_es,
)


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
    video = contact.get("telehealth_ok")
    fixes = {  # barriers with a direct fix: (referral, reason, talking point)
        "transport_barrier": (
            "transport",
            "patient has no transportation to the follow-up visit",
            "we can arrange a ride, or do a video visit from home" if video else "we can arrange a ride",
        ),
        "cost_barrier": ("social_work", "patient worried about the cost of the visit", "a social worker can help"),
    }
    for barrier, (ref, reason, point) in fixes.items():
        if barrier not in barriers:
            continue
        plan["talking_points"].append(point)
        try:
            await gw.call(
                "referrals",
                "create_referral",
                case_id=state["case_id"],
                referral_type=ref,
                reason=reason,
                idempotency_key=f"{state['case_id']}:ref:{ref}",  # one referral per type per case
            )
            plan["referrals_filed"].append(ref)
        except ToolFailed:
            pass
    return {"plan": plan}


SPECIFIC_TIME = (
    r"\b(yesterday|today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|"
    r"afternoon|evening|\d{1,2}\s*(am|pm)|\d{1,2}:\d{2}|dr\.?|ayer|hoy|mañana|lunes|martes|miércoles|jueves|viernes|"
    r"sábado|domingo|tarde|noche)\b"
)
STUB_INTENTS = [  # keyword rules, first match wins: enough for free simulation runs, not for production
    (  # first: a symptom inside a yes still goes to a nurse ("yes the first one, my legs are a bit puffy")
        "symptom",
        (
            r"\b(swollen|swelling|swell\w*|puff\w*|hinchad\w*|dizzy|mareado|fever|fiebre|short of breath"
            r"|shoes (are|feel) tight|can'?t catch my breath|gained \d+ (pounds|lbs?|kilos?)"
            r"|breathing (feels|is|got) (heavier|harder|worse))\b"
        ),
    ),
    (
        "clinical_question",
        (
            r"\b(can i take|should i (take|stop|skip|double)|is (it|this) normal|medications?|medicine|pills?|doses?|"
            r"pastillas|medicina|dosis)\b"
        ),
    ),
    (
        "barrier",
        (
            r"\b(ride|transport\w*|no car|can'?t get there|afford|copay|co-pay|too expensive|money|traslado"
            r"|transporte|no tengo (carro|coche)|pagar|dinero|copago)\b"
        ),
    ),
    ("wants_human", r"\b(nurse|real person|call me|enfermera)\b"),
    ("abusive", r"\b(useless|idiots?|stupid|regret it|hurt (you|someone|somebody)|kill (you|someone))\b"),
]


VIDEO = r"\b(video|virtual|from home|por video|desde casa|videollamada)\b"
WORKS = r"\bwork(s|ing)? (in the )?(mornings?|afternoons?)\b|\btrabajo (por la|en las) (mañanas?|tardes?)\b"
STANDING = r"\b(always|usually|every|never|work|siempre|nunca|trabajo)\b"


def scheduling_details(t: str) -> tuple[dict, bool]:
    """Visit type and time of day a reply asks for, and whether it's a standing preference. "I work mornings"
    means afternoons; "afternoons" or "por la tarde" means afternoons."""
    details: dict = {}
    if re.search(VIDEO, t):
        details["mode"] = "telehealth"
    if m := re.search(WORKS, t):
        details["part_of_day"] = "afternoon" if re.search(r"morning|mañana", m.group(0)) else "morning"
    elif re.search(r"\bafternoons?\b|\bpor la tarde\b|\bafter lunch\b", t):
        details["part_of_day"] = "afternoon"
    elif re.search(r"\bmornings?\b|\bpor la mañana\b", t):
        details["part_of_day"] = "morning"
    return details, bool(details.get("part_of_day")) and bool(re.search(STANDING, t))


async def reply_interpreter(state: TurnState, gw: Gateway) -> dict:
    t = (state.get("inbound") or "").lower()
    for name, pattern in STUB_INTENTS:
        if re.search(pattern, t):
            details = {"barrier": (state.get("inbound") or "")[:200]} if name == "barrier" else {}
            return {"intent": {"intent": name, "accepts": False, "details": details}}
    details, standing = scheduling_details(t)
    if picked_option(t) is not None:  # "the second one", "2"
        intent = {"intent": "wants_visit", "accepts": True, "details": details}
    elif re.search(r"\b(yes|book|sure|ok|okay|first|works|sí|si|primera|primero)\b", t):
        # a yes with its own time or day ("book me yesterday at 3 AM") is not a yes to our options: offer, don't book
        accepts = picked_option(t) is not None or not re.search(SPECIFIC_TIME, t)
        intent = {"intent": "wants_visit", "accepts": accepts, "details": details}
    elif re.search(r"\b(no thanks|not interested|don'?t need)\b", t):
        intent = {"intent": "decline", "accepts": False, "details": {}}
    elif details:  # "afternoons are always better", "could we do video?": a request for times
        intent = {"intent": "wants_visit", "accepts": False, "details": details, "standing": standing}
    elif "?" in t:
        intent = {"intent": "question", "accepts": False, "details": {}}
    else:
        intent = {"intent": "unclear", "accepts": False, "details": {}}
    return {"intent": intent}


async def picked_offer(state: TurnState, gw: Gateway) -> dict | None:
    """The option the patient picked from the times we last sent ("the second one"), looked up in the store,
    not re-searched: the list may have changed since. None if they didn't pick a numbered option."""
    intent = state.get("intent") or {}
    if state.get("trigger") != "inbound_reply" or intent.get("intent") != "wants_visit" or not intent.get("accepts"):
        return None
    n = picked_option(state.get("inbound"))
    if n is None:
        return None
    offers = await gw.call("scheduling", "get_offered_slots", case_id=state["case_id"]) or []
    if not offers:
        return None
    pick = offers[n - 1] if 1 <= n <= len(offers) else (offers[-1] if n == -1 else None)
    return {"option": n, "slot": pick}


async def _fitting(gw: Gateway, case_id: str, details: dict, now: str) -> list[dict]:
    """Up to 3 open slots that fit `details` (visit type, days, time of day, time bounds)."""
    earliest, latest = search_window(details, now)
    found = (
        await gw.call(
            "scheduling",
            "find_slots",
            case_id=case_id,
            earliest=earliest,
            latest=latest,
            mode=details.get("mode"),
            part_of_day=details.get("part_of_day"),
            limit=10,
        )
        or []
    )
    return [s for s in found if not slot_fits(s["start_at"], details)][:3]


def _usable(details: dict, telehealth_ok: bool) -> dict:
    """A video request from a patient who can't do video (no consent or device) can't be searched for."""
    if details.get("mode") == "telehealth" and not telehealth_ok:
        return {k: v for k, v in details.items() if k != "mode"}
    return details


async def scheduler(state: TurnState, gw: Gateway) -> dict:
    case_id, intent = state["case_id"], state.get("intent") or {}
    telehealth_ok = bool(state["summary"]["contact"].get("telehealth_ok"))
    asked = request_details(state)  # this reply on top of standing preferences
    no_video = asked.get("mode") == "telehealth" and not telehealth_ok  # the message must say why: clinic only
    details = _usable(asked, telehealth_ok)
    note = None

    def result(offered, appointment, note):
        return {
            "schedule": {
                "offered": offered,
                "appointment": appointment,
                "error": None,
                "note": note,
                "video_unavailable": no_video,
            }
        }

    try:
        picked = await picked_offer(state, gw)
        slot = picked["slot"] if picked else None
        other_mode = bool(slot and details.get("mode") and slot["mode"] != details["mode"])
        if slot and slot["still_open"] and not other_mode:
            appointment = await gw.call(
                "scheduling",
                "book_appointment",
                case_id=case_id,
                slot_id=slot["slot_id"],
                idempotency_key=f"{state['turn_key']}:book",
            )
            appointment["when"] = when(appointment["start_at"])
            appointment["provider"] = slot["provider"]
            return result([], appointment, None)
        if slot and slot["still_open"]:  # "a video visit would work, the first one is fine" when the first is clinic
            note = f"the patient asked for a {how(details['mode'])}, not the time they picked; offer these instead"
        elif picked:  # they picked a time that's gone (taken, or too soon now): never book a different one for them
            note = (
                f"the time the patient picked ({when(slot['start_at'])}) is no longer open; offer these instead"
                if slot
                else f"the patient picked option {picked['option']}, which we didn't offer; offer these"
            )
            # "the first one" only named that time: what an interpreter read into it (its day, its time of day) is
            # not a new request. Search again with the standing preferences, and a visit type they asked for.
            details = _usable(
                standing_details(state) | ({"mode": details["mode"]} if details.get("mode") else {}), telehealth_ok
            )
        fitting = await _fitting(gw, case_id, details, state["now"])
        if not fitting:  # nothing matches this request: keep the standing preferences if any, then anything
            standing = _usable(standing_details(state), telehealth_ok)
            if standing and standing != details:
                fitting = await _fitting(gw, case_id, standing, state["now"])
            if not fitting:
                fitting = await gw.call("scheduling", "find_slots", case_id=case_id, limit=3) or []
            note = note or "no open times match what the patient asked for; these are the closest options"
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
    return result(offered, appointment, note)


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
        "gone": "Sorry {name}, that time is no longer open. Here are new ones.",
        "intro": "Hi {name}, this is your care team at Orchard Valley. We'd like to see you for a follow-up visit "
        "after your hospital stay.",
        "options": "Open times: {options}. Reply with the number that works for you.",
        "channel_ok": "Thanks {name}, we'll use this channel from now on.",
        "language_ok": "Thanks {name}, we'll write to you in English from now on.",
        "channel_no": "Thanks {name}. We can't switch to that channel yet. Reply NURSE if you'd like a nurse to help "
        "update your contact details.",
        "barrier": "Thanks for telling us, {name}. We can help: a video visit from home is an option, and our team "
        "can arrange a ride. Would you like us to find a time?",
        "barrier_ride": "Thanks for telling us, {name}. We can help: our team can arrange a ride to your visit. "
        "Would you like us to find a time?",
        "barrier_cost": "Thanks for telling us, {name}. A social worker from our team can help with the cost of the "
        "visit. Would you like us to find a time?",
        "barrier_other": "Thanks for telling us, {name}. We'd like to make this visit work for you. Would you like "
        "us to find a time?",
        "decline": "Thanks for letting us know, {name}. If you change your mind, just reply to this message.",
        "question": "Thanks for your question, {name}. A nurse can help with it: reply NURSE if you'd like a call.",
        "no_video": "Video visits aren't set up for you yet, so this is a clinic visit.",
        "ride": "Our team is arranging your ride.",
        "unclear": "Sorry {name}, we didn't quite understand. Would you like us to book a follow-up visit? "
        "Reply YES or NO.",
    },
    "Spanish": {
        "subject": "Su visita de seguimiento",
        "booked": "Hola {name}, su visita de seguimiento está reservada para el {when} ({how}).",
        "no_show": "Hola {name}, le extrañamos en su visita, no se preocupe. Busquemos una nueva hora.",
        "clinic_cancelled": "Hola {name}, lo sentimos: la clínica tuvo que cancelar su visita. Busquemos una nueva "
        "hora.",
        "gone": "Lo sentimos {name}, esa hora ya no está disponible. Aquí tiene otras.",
        "intro": "Hola {name}, le escribe su equipo de Orchard Valley. Queremos verle en una visita de seguimiento "
        "tras su hospitalización.",
        "options": "Horarios: {options}. Responda con el número que le funcione.",
        "channel_ok": "Gracias {name}, usaremos este medio desde ahora.",
        "language_ok": "Gracias {name}, desde ahora le escribiremos en español.",
        "channel_no": "Gracias {name}. Todavía no podemos usar ese medio. Responda ENFERMERA si quiere que una "
        "enfermera le ayude a actualizar sus datos de contacto.",
        "barrier": "Gracias por contarnos, {name}. Podemos ayudar: una visita por video desde casa es una opción, y "
        "nuestro equipo puede organizar transporte. ¿Quiere que busquemos una hora?",
        "barrier_ride": "Gracias por contarnos, {name}. Podemos ayudar: nuestro equipo puede organizar transporte a "
        "su visita. ¿Quiere que busquemos una hora?",
        "barrier_cost": "Gracias por contarnos, {name}. Una trabajadora social de nuestro equipo puede ayudarle con "
        "el costo de la visita. ¿Quiere que busquemos una hora?",
        "barrier_other": "Gracias por contarnos, {name}. Queremos que esta visita funcione para usted. ¿Quiere que "
        "busquemos una hora?",
        "decline": "Gracias por avisarnos, {name}. Si cambia de opinión, responda a este mensaje.",
        "question": "Gracias por su pregunta, {name}. Una enfermera puede ayudarle: responda ENFERMERA si quiere "
        "que le llamen.",
        "no_video": "Las visitas por video aún no están disponibles para usted, así que esta es una visita en la "
        "clínica.",
        "ride": "Nuestro equipo está organizando su transporte.",
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
    barriers = set(((state["summary"].get("risk") or {}).get("barriers")) or [])
    rides = "transport" in on_file(state) and "transport_barrier" in barriers  # only what's arranged (ADR 0011)
    if sched.get("appointment"):
        w, h = show(sched["appointment"])
        body = t["booked"].format(name=name, when=w, how=h)
        if sched.get("video_unavailable"):
            body += " " + t["no_video"]
        if rides and sched["appointment"].get("mode") != "telehealth":
            body += " " + t["ride"]
    elif sched.get("offered"):
        options = "; ".join(f"{i}) {w} ({h})" for i, (w, h) in enumerate(map(show, sched["offered"]), 1))
        gone = "no longer open" in (sched.get("note") or "")
        opening = t["gone"] if gone else t.get((state.get("case") or {}).get("rebook_reason") or "intro") or t["intro"]
        if intent == "language_change":  # confirm the switch, then the times again in the new language
            opening = t["language_ok"]
        if sched.get("video_unavailable"):
            opening += " " + t["no_video"].replace("this is a clinic visit", "these are clinic visits").replace(
                "esta es una visita en la clínica", "estas son visitas en la clínica"
            )
        body = f"{opening.format(name=name)} {t['options'].format(options=options)}"
    elif intent == "channel_change":
        ok = (state.get("channel_change") or {}).get("ok")
        body = t["channel_ok" if ok else "channel_no"].format(name=name)
    elif intent == "language_change":
        body = t["language_ok"].format(name=name)
    elif intent == "barrier":
        this = state.get("barrier") or next(iter(barriers & {"transport_barrier", "cost_barrier"}), None)
        video = state["summary"]["contact"].get("telehealth_ok")
        key = {
            "transport_barrier": "barrier" if video else "barrier_ride",
            "cost_barrier": "barrier_cost",
        }.get(this, "barrier_other")
        if key in ("barrier", "barrier_ride") and "transport" not in on_file(state):
            key = "barrier_other"  # never offer a ride nobody has been asked to arrange
        body = t[key].format(name=name)
    elif intent in ("decline", "question"):
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
