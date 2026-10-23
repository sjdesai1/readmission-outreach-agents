"""The outreach graph: rule nodes + 5 agents, wired with LangGraph. One run = one turn for one patient.

load_context -> input_guard -> route
  phone reply, identity not yet confirmed -> identity_check (year of birth, checked server-side) -> continue / ask
  outreach_due  -> care_planner -> scheduler -> message_writer
  inbound_reply -> reply_interpreter -> remember (standing preferences) -> by intent:
        wants_visit / reschedule -> scheduler -> message_writer
        question / barrier       -> care_planner -> message_writer
        decline                  -> message_writer
        channel_change           -> apply_channel_change -> message_writer (answers on the new channel)
        opt_out                  -> opt_out
        third_party              -> proxy_reply  (family member: privacy-safe template, no LLM)
        symptom / clinical_question / wants_human / abusive / unclear (2nd time) -> escalate
message_writer -> output_checks -> safety_reviewer -> send      (one rewrite allowed, then escalate)

escalate = nurse handoff: a task with an honest due time (urgent within the hour, routine by 5 PM on the next
business day with capacity), a fixed acknowledgement to the patient, and the AI pauses on the case until a
nurse hands it back. The AI never answers clinical questions itself. While paused, a patient reply still goes
through input_guard: STOP and emergencies are handled, anything else is flagged for the nurse (hold_for_nurse).
"""

import hashlib
import re
from datetime import timedelta

from langgraph.graph import END, START, StateGraph

from readmit import llm
from readmit.agents.gateway import Gateway, ToolFailed
from readmit.agents.guards import check_draft, screen_inbound
from readmit.agents.privacy import configure_tracing, pseudonymize, pseudonymize_summary, render
from readmit.agents.state import MAX_REVISIONS, TurnState, parse_dt, reply_channel, when, when_es
from readmit.agents.stubs import STUBS
from readmit.config import settings
from readmit.outreach import cadence

CLOSED = {"suppressed", "completed", "opted_out", "readmitted", "closed"}
PAUSED = {"escalated"}  # a nurse has the case: the AI stays quiet, but STOP and emergencies are still handled
RECENT = 6  # messages kept word for word; older ones become a one-line digest
FOLLOW_UP_AFTER = timedelta(hours=48)

EMERGENCY_MSG = {
    "English": "If you are having a medical emergency, call 911 now. A nurse from your care team will contact "
    "you shortly.",
    "Spanish": "Si tiene una emergencia médica, llame al 911 ahora. Una enfermera de su equipo de atención se "
    "comunicará con usted pronto.",
}
PROXY_MSG = {
    "English": "Thank you for replying. For privacy, we can only discuss this with the person this message was "
    "sent to. Please ask them to reply here or contact their care team.",
    "Spanish": "Gracias por responder. Por privacidad, solo podemos hablar de esto con la persona a quien se "
    "envió este mensaje. Pídale que responda aquí o que se comunique con su equipo de atención.",
}
ID_QUESTION = {  # appended by code to every call script until identity is confirmed: exact wording, every time
    "English": "Before we go on, to protect your privacy, could you please tell me your year of birth?",
    "Spanish": "Antes de continuar, para proteger su privacidad, ¿podría decirme su año de nacimiento?",
}
ID_RETRY = {
    "English": "Sorry, that didn't match our records. Could you please tell me your year of birth again?",
    "Spanish": "Lo siento, eso no coincide con nuestros registros. ¿Podría decirme su año de nacimiento otra vez?",
}
YEAR_RE = re.compile(r"\b(19\d{2}|20[0-2]\d)\b")
SYMPTOM_ACK = {
    "English": "Thank you for telling us. A nurse from your care team will contact you within the hour. "
    "If it gets worse, or you think it is an emergency, call 911.",
    "Spanish": "Gracias por avisarnos. Una enfermera de su equipo de atención se comunicará con usted en la "
    "próxima hora. Si empeora, o cree que es una emergencia, llame al 911.",
}
CALLBACK_ACK = {  # the time is the nurse task's real due time, never a promise the queue can't keep
    "English": "Thank you. A nurse from your care team will contact you by {due}. "
    "If anything gets worse before then, call your doctor's office, or 911 in an emergency.",
    "Spanish": "Gracias. Una enfermera de su equipo de atención se comunicará con usted antes del {due}. "
    "Si algo empeora antes, llame al consultorio de su médico, o al 911 en una emergencia.",
}
MEMORY_KEYS = ("part_of_day", "earliest_time", "latest_time", "mode", "preferred_days")
OPT_OUT_MSG = {
    "English": "You will not receive more outreach messages from the Orchard Valley care team. "
    "For medical questions, contact your doctor's office.",
    "Spanish": "No recibirá más mensajes del equipo de atención de Orchard Valley. "
    "Para preguntas médicas, comuníquese con el consultorio de su médico.",
}


# ------------------------------------------------------------------ rule nodes
async def load_context(state: TurnState, gw: Gateway) -> dict:
    cid = state["case_id"]
    now = await gw.call("scheduling", "get_current_time")
    case = await gw.call("cases", "get_case", case_id=cid)
    if case["status"] in CLOSED or (case["status"] in PAUSED and state["trigger"] != "inbound_reply"):
        return {"now": now, "case": case, "skip": f"case is {case['status']}"}
    if parse_dt(case["opened_at"]) > parse_dt(now):
        return {"now": now, "case": case, "skip": "case not open yet"}
    if state["trigger"] == "outreach_due" and case["status"] == "booked":
        return {"now": now, "case": case, "skip": "already booked"}
    if state["trigger"] == "reminder" and case["status"] != "booked":
        return {"now": now, "case": case, "skip": f"no reminder: case is {case['status']}"}
    # the patient's name never enters the state: agents, prompts and traces see {first_name} instead
    summary, name = pseudonymize_summary(await gw.call("patient_data", "get_patient_summary", case_id=cid))
    thread = [
        {**m, "body": pseudonymize(m["body"], name)}
        for m in await gw.call("messaging", "get_thread", case_id=cid, limit=50) or []
    ]
    step = None
    if state["trigger"] == "outreach_due":
        # after a no-show or a clinic cancellation the cadence starts again from that moment
        since = case.get("rebook_at") or ""
        recent = [m for m in thread if m["at"] >= since] if since else thread
        step = cadence.next_attempt(cadence.unanswered(recent), summary["contact"]["allowed_channels"])
    older, recent = thread[:-RECENT], thread[-RECENT:]
    history = ""
    if older:
        n_out = sum(m["direction"] == "outbound" for m in older)
        history = (
            f"{len(older)} earlier messages ({n_out} from us, {len(older) - n_out} from the patient), "
            f"from {older[0]['at'][:10]} to {older[-1]['at'][:10]}"
        )
    streak = 0
    for e in reversed(case.get("recent_events", [])):
        if e["type"] != "note":
            continue
        if str(e["detail"].get("note", "")).startswith("intent: unclear"):
            streak += 1
        else:
            break
    seed = f"{cid}|{state['trigger']}|{now}|{state.get('inbound') or ''}"
    turn_key = f"{cid}:{hashlib.sha256(seed.encode()).hexdigest()[:10]}"
    return {
        "now": now,
        "case": case,
        "summary": summary,
        "thread": recent,
        "history": history,
        "unclear_streak": streak,
        "turn_key": turn_key,
        "skip": None,
        "revisions": 0,
        "inbound": pseudonymize(state.get("inbound"), name),
        "paused": case["status"] in PAUSED,
        "cadence": step,
    }


async def input_guard(state: TurnState, gw: Gateway) -> dict:
    g = screen_inbound(state.get("inbound"))
    # anyone can answer a phone: until identity is confirmed, phone replies go through the identity check
    # (STOP and emergencies are still handled first, whoever is speaking)
    if (
        g["action"] == "continue"
        and state.get("inbound_channel") == "phone"
        and not (state.get("case") or {}).get("identity_verified_at")
    ):
        g = {**g, "action": "verify_identity"}
    out = {"guard": g}
    if g["action"] == "emergency":
        out |= {"escalate_reason": f"possible emergency in patient reply: {state.get('inbound')!r}", "urgent": True}
    return out


async def output_checks(state: TurnState, gw: Gateway) -> dict:
    sched = state.get("schedule") or {}
    stay = (state.get("summary") or {}).get("stay") or {}
    channel = reply_channel(state)
    # the portal is logged in (identity verified), so clinical details may appear there; nowhere else
    private = [] if channel == "portal" else [stay.get("diagnosis_group")]
    problems = check_draft(
        state["draft"]["body"], channel, sched.get("offered") or [], sched.get("appointment"), private_terms=private
    )
    if problems:
        return {"problems": problems, "revisions": state.get("revisions", 0) + 1}
    return {"problems": []}


async def send(state: TurnState, gw: Gateway) -> dict:
    cid, channel, draft = state["case_id"], reply_channel(state), state["draft"]
    intent = (state.get("intent") or {}).get("intent")
    try:
        # the real name is looked up only here, at the last moment, and never written back into the state
        name = (await gw.call("patient_data", "get_patient_summary", case_id=cid))["patient"]["first_name"]
        body = render(draft["body"], name)
        lang = state["summary"]["patient"]["preferred_language"]
        if (
            channel == "phone"
            and not (state.get("case") or {}).get("identity_verified_at")
            and not (state.get("identity") or {}).get("verified")
        ):
            body = f"{body} {ID_QUESTION.get(lang, ID_QUESTION['English'])}"
        msg = await gw.call(
            "messaging",
            "send_message",
            case_id=cid,
            channel=channel,
            body=body,
            subject=render(draft["subject"], name) if draft.get("subject") else None,
            idempotency_key=f"{state['turn_key']}:msg",
        )
        sched = state.get("schedule") or {}
        if sched.get("offered") and not sched.get("appointment"):  # remember what "the first one" means
            await gw.call(
                "scheduling",
                "record_offers",
                case_id=cid,
                slot_ids=[s["slot_id"] for s in sched["offered"]],
                message_id=msg["message_id"],
            )
        if intent:
            await gw.call("cases", "add_case_note", case_id=cid, note=f"intent: {intent}")
        if intent == "decline":
            await gw.call("cases", "update_case_status", case_id=cid, status="closed", reason="patient declined")
        elif not (state.get("schedule") or {}).get("appointment"):
            nxt = (parse_dt(state["now"]) + FOLLOW_UP_AFTER).isoformat(timespec="minutes")
            await gw.call(
                "cases",
                "update_case_status",
                case_id=cid,
                status="awaiting_reply",
                reason=f"message sent by {channel}",
                next_action_at=nxt,
            )
    except ToolFailed as e:
        return {"escalate_reason": f"send failed: {e.message}"}
    held = msg["status"] == "scheduled"  # quiet hours: the server holds it until 8 AM
    return {
        "outcome": {
            "action": "scheduled" if held else "sent",
            "channel": channel,
            "message_id": msg["message_id"],
            **({"deliver_at": msg["deliver_at"]} if held else {}),
        }
    }


def _handoff(state: TurnState) -> tuple[str, str, str | None]:
    """(task kind, priority, which acknowledgement the patient gets). Decided by code, never by an LLM."""
    if state.get("urgent"):
        return "emergency", "urgent", None  # the 911 message is sent separately, first
    intent = (state.get("intent") or {}).get("intent")
    replied = state.get("trigger") == "inbound_reply" and bool(state.get("inbound_channel"))
    if intent == "symptom":
        return "symptom", "urgent", "symptom" if replied else None
    if intent == "clinical_question":
        return "clinical_question", "routine", "callback" if replied else None
    if intent in ("wants_human", "unclear"):
        return "callback", "routine", "callback" if replied else None
    identity_failed = state.get("identity") is not None and not state["identity"].get("verified")
    if intent == "abusive" or identity_failed:
        return "review", "routine", None  # no reply: nothing useful or safe to say
    # system trouble (draft failed twice, LLM down, no channel...): the patient who wrote still hears back
    return "review", "routine", "callback" if replied else None


async def escalate(state: TurnState, gw: Gateway) -> dict:
    cid = state["case_id"]
    reason = state.get("escalate_reason") or _why_escalate(state)
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    kind, priority, ack = _handoff(state)
    safety_id = ack_id = task = None
    if kind == "emergency" and state.get("inbound_channel"):  # safety first: before anything else
        try:
            sent = await gw.call(
                "messaging",
                "send_message",
                case_id=cid,
                channel=state["inbound_channel"],
                body=EMERGENCY_MSG.get(lang, EMERGENCY_MSG["English"]),
                priority="safety",
                idempotency_key=f"{state['turn_key']}:safety",
            )
            safety_id = sent["message_id"]
        except ToolFailed:
            pass  # the nurse still gets the case; never let a failed send hide an emergency
    try:
        task = await gw.call(
            "cases",
            "create_nurse_task",
            case_id=cid,
            kind=kind,
            priority=priority,
            reason=reason[:300],
            summary=(state.get("inbound") or "")[:300] or None,
            idempotency_key=f"{state['turn_key']}:nurse",
        )
    except ToolFailed as e:  # the queue is down: still pause the AI and leave a note a human will see
        reason = f"{reason} (nurse task failed: {e.message})"
        await gw.call("cases", "update_case_status", case_id=cid, status="escalated", reason=reason[:300])
    if ack and task:
        if ack == "symptom":
            body, prio = SYMPTOM_ACK.get(lang, SYMPTOM_ACK["English"]), "safety"
        else:
            due = (when_es if lang == "Spanish" else when)(task["due_at"])
            body, prio = CALLBACK_ACK.get(lang, CALLBACK_ACK["English"]).format(due=due), "normal"
        try:
            sent = await gw.call(
                "messaging",
                "send_message",
                case_id=cid,
                channel=state["inbound_channel"],
                body=body,
                priority=prio,
                idempotency_key=f"{state['turn_key']}:ack",
            )
            ack_id = sent["message_id"]
        except ToolFailed:
            pass  # the task exists; the nurse will reach out regardless
    await gw.call("cases", "add_case_note", case_id=cid, note=f"escalated ({kind}, {priority}): {reason}")
    return {
        "escalate_reason": reason,
        "nurse_task": task,
        "outcome": {
            "action": "escalated",
            "urgent": priority == "urgent",
            "reason": reason,
            "nurse_task": {k: task[k] for k in ("task_id", "kind", "priority", "due_at")} if task else None,
            "safety_message_id": safety_id,
            "ack_message_id": ack_id,
        },
    }


REMINDER_MSG = {
    "English": "Hi {first_name}, a reminder from your care team at Orchard Valley: your follow-up visit is "
    "{when}, {how} with {doctor}. If you need to change it, just let us know.",
    "Spanish": "Hola {first_name}, un recordatorio de su equipo de atención de Orchard Valley: su visita de "
    "seguimiento es el {when}, {how} con {doctor}. Si necesita cambiarla, avísenos.",
}
HOW_ES = {"video visit": "visita por video", "clinic visit": "visita en la clínica"}


async def remind(state: TurnState, gw: Gateway) -> dict:
    """Day-before reminder: a fixed template with the real time, visit type and doctor. No LLM."""
    cid = state["case_id"]
    appt = await gw.call("scheduling", "get_appointment", case_id=cid)
    if not appt:
        return {"outcome": {"action": "no_reminder", "reason": "no upcoming visit"}}
    contact = state["summary"]["contact"]
    channel = contact["preferred_channel"] if contact["preferred_channel"] in contact["allowed_channels"] else None
    channel = channel or (contact["allowed_channels"] or [None])[0]
    if not channel:
        return {"outcome": {"action": "no_reminder", "reason": "no consented channel"}}
    lang = state["summary"]["patient"]["preferred_language"]
    how_ = "video visit" if appt["mode"] == "telehealth" else "clinic visit"
    name = (await gw.call("patient_data", "get_patient_summary", case_id=cid))["patient"]["first_name"]
    body = REMINDER_MSG.get(lang, REMINDER_MSG["English"]).format(
        first_name=name,
        when=(when_es if lang == "Spanish" else when)(appt["start_at"]),
        how=HOW_ES[how_] if lang == "Spanish" else how_,
        doctor=appt["provider"],
    )
    msg = await gw.call(
        "messaging",
        "send_message",
        case_id=cid,
        channel=channel,
        body=body,
        subject="Reminder: your follow-up visit" if channel in ("email", "portal") else None,
        idempotency_key=f"{appt['appointment_id']}:reminder",  # one reminder per visit, however often we run
    )
    return {"outcome": {"action": "reminded", "channel": channel, "message_id": msg["message_id"]}}


async def close_unreachable(state: TurnState, gw: Gateway) -> dict:
    """The cadence ran out with no reply: close the case. A nurse can reopen it."""
    reason = state["cadence"]["reason"]
    await gw.call("cases", "add_case_note", case_id=state["case_id"], note=f"closed: {reason}")
    await gw.call("cases", "update_case_status", case_id=state["case_id"], status="closed", reason=reason)
    return {"outcome": {"action": "closed_unreachable", "reason": reason}}


async def hold_for_nurse(state: TurnState, gw: Gateway) -> dict:
    """A nurse has this case. The AI doesn't reply; the message is flagged on the case for the nurse."""
    await gw.call(
        "cases", "add_case_note", case_id=state["case_id"], note="patient wrote while with a nurse; no AI reply"
    )
    return {"outcome": {"action": "held_for_nurse"}}


async def remember(state: TurnState, gw: Gateway) -> dict:
    """Save what the patient told us that should outlast this turn:
    - a barrier ("I don't have a ride"), so the planner can act on it now and on later turns
    - standing preferences ("I work mornings"), only when the interpreter marked them as standing, known keys only
    """
    intent = state.get("intent") or {}
    details = intent.get("details") or {}
    out: dict = {}
    if intent.get("intent") == "barrier" and details.get("barrier"):
        try:
            b = await gw.call("cases", "record_barrier", case_id=state["case_id"], detail=details["barrier"])
            risk = dict((state.get("summary") or {}).get("risk") or {})
            risk["barriers"] = sorted({*(risk.get("barriers") or []), b["barrier"]})
            out = {"summary": {**state["summary"], "risk": risk}}
        except ToolFailed:
            pass  # the planner still sees the barrier in this turn's intent
    if not intent.get("standing"):
        return out
    memory = dict((state.get("summary") or {}).get("memory") or {})
    saved = []
    for key in MEMORY_KEYS:
        value = details.get(key)
        if value in (None, [], "") or memory.get(key) == value:
            continue  # nothing new: don't re-save, and don't tell the patient "we'll remember" again
        try:
            await gw.call(
                "patient_data",
                "remember_preference",
                case_id=state["case_id"],
                key=key,
                value=value,
                source=(state.get("inbound") or "")[:200],
            )
        except ToolFailed:
            continue  # memory is a convenience, never a reason to stop the turn
        memory[key] = value
        saved.append(key)
    if not saved:
        return out
    summary = out.get("summary") or state["summary"]
    return {"remembered": saved, "summary": {**summary, "memory": memory}}


async def opt_out(state: TurnState, gw: Gateway) -> dict:
    cid = state["case_id"]
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    channel = state.get("inbound_channel")
    if channel:  # one confirmation, sent before the case closes (closed cases can't be messaged)
        try:
            await gw.call(
                "messaging",
                "send_message",
                case_id=cid,
                channel=channel,
                body=OPT_OUT_MSG.get(lang, OPT_OUT_MSG["English"]),
                priority="safety",
                idempotency_key=f"{state['turn_key']}:optout",
            )
        except ToolFailed:
            pass
    await gw.call("cases", "update_case_status", case_id=cid, status="opted_out", reason="patient opted out")
    return {"outcome": {"action": "opted_out"}}


async def identity_check(state: TurnState, gw: Gateway) -> dict:
    """Phone reply before identity is confirmed: look for a year of birth and check it server-side."""
    cid, lang = state["case_id"], state["summary"]["patient"]["preferred_language"]
    year = YEAR_RE.search(state.get("inbound") or "")
    if year:
        try:
            r = await gw.call("patient_data", "verify_identity", case_id=cid, birth_year=int(year.group(1)))
        except ToolFailed as e:
            return {"identity": {"verified": False}, "escalate_reason": f"identity check locked: {e.message}"}
        if r["verified"]:
            return {"identity": {"verified": True}}  # carry on with the rest of what they said
        if r["attempts_left"] == 0:
            return {
                "identity": {"verified": False},
                "escalate_reason": "identity check failed 3 times; a nurse must verify the patient",
            }
        body, note = ID_RETRY.get(lang, ID_RETRY["English"]), "identity: answer didn't match, asked again"
    else:
        body, note = ID_QUESTION.get(lang, ID_QUESTION["English"]), "identity: asked for year of birth"
    try:
        msg = await gw.call(
            "messaging",
            "send_message",
            case_id=cid,
            channel="phone",
            body=body,
            idempotency_key=f"{state['turn_key']}:identity",
        )
    except ToolFailed as e:
        return {"identity": {"verified": False}, "escalate_reason": f"identity question could not be sent: {e.message}"}
    await gw.call("cases", "add_case_note", case_id=cid, note=note)
    return {
        "identity": {"verified": False, "asked": True},
        "outcome": {"action": "identity_requested", "message_id": msg["message_id"]},
    }


async def apply_channel_change(state: TurnState, gw: Gateway) -> dict:
    """The patient asked to be contacted another way: record it (server checks the contact exists), answer there."""
    new = ((state.get("intent") or {}).get("details") or {}).get("new_channel")
    if not new:
        return {"channel_change": {"ok": False, "reason": "patient didn't say which channel"}}
    try:
        contact = await gw.call(
            "cases",
            "set_preferred_channel",
            case_id=state["case_id"],
            channel=new,
            reason=f"patient asked: {(state.get('inbound') or '')[:120]}",
        )
    except ToolFailed as e:
        return {"channel_change": {"ok": False, "requested": new, "reason": e.message}}
    summary = {**state["summary"], "contact": contact}
    return {"channel_change": {"ok": True, "now_using": new}, "reply_on": new, "summary": summary}


async def proxy_reply(state: TurnState, gw: Gateway) -> dict:
    """Someone other than the patient replied. Fixed template: no name, no health details, no booking."""
    cid = state["case_id"]
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    try:
        msg = await gw.call(
            "messaging",
            "send_message",
            case_id=cid,
            channel=state["inbound_channel"],
            body=PROXY_MSG.get(lang, PROXY_MSG["English"]),
            idempotency_key=f"{state['turn_key']}:proxy",
        )
    except ToolFailed as e:
        return {"escalate_reason": f"third-party reply, and the privacy reply failed: {e.message}"}
    await gw.call("cases", "add_case_note", case_id=cid, note="third party replied; no details shared")
    nxt = (parse_dt(state["now"]) + FOLLOW_UP_AFTER).isoformat(timespec="minutes")
    await gw.call(
        "cases",
        "update_case_status",
        case_id=cid,
        status="awaiting_reply",
        reason="third party replied; waiting for the patient",
        next_action_at=nxt,
    )
    return {"outcome": {"action": "proxy_reply", "message_id": msg["message_id"]}}


def _why_escalate(state: TurnState) -> str:
    if state.get("problems"):
        return "draft failed checks twice: " + "; ".join(state["problems"])
    review = state.get("review") or {}
    if review.get("verdict") in ("escalate", "revise"):
        return "safety reviewer: " + "; ".join(review.get("reasons") or ["no reason given"])
    if (state.get("schedule") or {}).get("error"):
        return "scheduling failed: " + state["schedule"]["error"]
    intent = (state.get("intent") or {}).get("intent")
    if intent:
        asked = ((state.get("intent") or {}).get("details") or {}).get("question")
        return f"patient reply needs a nurse ({intent})" + (f": {asked}" if asked else "")
    if state.get("plan") and not state["plan"].get("channel"):
        return "no channel the patient consented to"
    return "unspecified"


# ------------------------------------------------------------------ routing (pure functions of state)
def after_context(state: TurnState) -> str:
    if state.get("skip"):
        return END
    if (state.get("cadence") or {}).get("close"):
        return "close_unreachable"
    if state["trigger"] == "reminder":
        return "remind"
    return "input_guard" if state["trigger"] == "inbound_reply" else "care_planner"


def after_guard(state: TurnState) -> str:
    if state.get("paused") and state["guard"]["action"] not in ("opt_out", "emergency"):
        return "hold_for_nurse"
    return {
        "opt_out": "opt_out",
        "emergency": "escalate",
        "third_party": "proxy_reply",
        "verify_identity": "identity_check",
    }.get(state["guard"]["action"], "reply_interpreter")


def after_identity(state: TurnState) -> str:
    if state.get("escalate_reason"):
        return "escalate"
    return "reply_interpreter" if state["identity"].get("verified") else END


SCHEDULING_DETAILS = ("mode", "preferred_days", "part_of_day", "earliest_time", "latest_time", "choice")


def after_interpreter(state: TurnState) -> str:
    if state.get("escalate_reason"):
        return "escalate"
    intent = state["intent"]["intent"]
    details = state["intent"].get("details") or {}
    if intent in ("wants_visit", "reschedule"):
        return "scheduler"
    if intent == "question" and any(details.get(k) for k in SCHEDULING_DETAILS):
        return "scheduler"  # "do you have clinic visits?" needs real slots, not a promise
    if intent in ("question", "barrier"):
        return "care_planner"
    if intent == "channel_change":
        return "apply_channel_change"
    if intent == "opt_out":
        return "opt_out"
    if intent == "third_party":
        return "proxy_reply"
    if intent in ("symptom", "abusive", "clinical_question", "wants_human"):
        return "escalate"
    if intent == "unclear" and state.get("unclear_streak", 0) >= 1:
        return "escalate"
    return "message_writer"


def after_planner(state: TurnState) -> str:
    if not reply_channel(state):
        return "escalate"
    return "scheduler" if state["trigger"] == "outreach_due" else "message_writer"


def after_scheduler(state: TurnState) -> str:
    s = state["schedule"]
    return "escalate" if s.get("error") and not s.get("offered") and not s.get("appointment") else "message_writer"


def after_checks(state: TurnState) -> str:
    if not state.get("problems"):
        return "safety_reviewer"
    return "message_writer" if state["revisions"] <= MAX_REVISIONS else "escalate"


def after_review(state: TurnState) -> str:
    verdict = state["review"]["verdict"]
    if verdict == "approve":
        return "send"
    if verdict == "revise" and state.get("revisions", 0) <= MAX_REVISIONS:
        return "message_writer"
    return "escalate"


def after_send(state: TurnState) -> str:
    return "escalate" if state.get("escalate_reason") else END


# ------------------------------------------------------------------ wiring
async def _reviewer_counted(fn, state, gw):
    out = await fn(state, gw)
    if out["review"]["verdict"] == "revise":
        out["revisions"] = state.get("revisions", 0) + 1
    return out


def build_graph(gateway: Gateway, agents: dict | None = None):
    agents = {**STUBS, **(agents or {})}
    rules = {
        "load_context": load_context,
        "input_guard": input_guard,
        "output_checks": output_checks,
        "send": send,
        "escalate": escalate,
        "opt_out": opt_out,
        "proxy_reply": proxy_reply,
        "apply_channel_change": apply_channel_change,
        "identity_check": identity_check,
        "remember": remember,
        "hold_for_nurse": hold_for_nurse,
        "close_unreachable": close_unreachable,
        "remind": remind,
    }
    reviewer = agents["safety_reviewer"]
    agents = {**agents, "safety_reviewer": lambda s, gw: _reviewer_counted(reviewer, s, gw)}

    def node(name, fn):
        async def run(state: TurnState) -> dict:
            return {**await fn(state, gateway.scoped(name)), "path": [name]}

        run.__name__ = name
        return run

    g = StateGraph(TurnState)
    for name, fn in {**rules, **agents}.items():
        g.add_node(name, node(name, fn))
    g.add_edge(START, "load_context")
    g.add_conditional_edges(
        "load_context", after_context, ["input_guard", "care_planner", "close_unreachable", "remind", END]
    )
    g.add_edge("close_unreachable", END)
    g.add_edge("remind", END)
    g.add_conditional_edges(
        "input_guard",
        after_guard,
        ["opt_out", "escalate", "proxy_reply", "identity_check", "reply_interpreter", "hold_for_nurse"],
    )
    g.add_edge("hold_for_nurse", END)
    g.add_conditional_edges("identity_check", after_identity, ["reply_interpreter", "escalate", END])
    g.add_edge("reply_interpreter", "remember")
    g.add_conditional_edges(
        "remember",
        after_interpreter,
        ["scheduler", "care_planner", "opt_out", "proxy_reply", "apply_channel_change", "escalate", "message_writer"],
    )
    g.add_edge("apply_channel_change", "message_writer")
    g.add_conditional_edges("care_planner", after_planner, ["scheduler", "message_writer", "escalate"])
    g.add_conditional_edges("scheduler", after_scheduler, ["message_writer", "escalate"])
    g.add_edge("message_writer", "output_checks")
    g.add_conditional_edges("output_checks", after_checks, ["safety_reviewer", "message_writer", "escalate"])
    g.add_conditional_edges("safety_reviewer", after_review, ["send", "message_writer", "escalate"])
    g.add_conditional_edges("send", after_send, ["escalate", END])
    g.add_edge("escalate", END)
    g.add_edge("opt_out", END)
    g.add_conditional_edges("proxy_reply", after_send, ["escalate", END])
    return g.compile()


_BUDGET_NOTICED = False


def _budget_notice() -> None:
    global _BUDGET_NOTICED
    if not _BUDGET_NOTICED:
        print(f"  LLM daily budget (${settings.llm_daily_budget_usd:.2f}) reached: rule-based agents until tomorrow")
        _BUDGET_NOTICED = True


async def run_turn(
    gateway: Gateway,
    case_id: str,
    trigger: str,
    inbound: str | None = None,
    inbound_channel: str | None = None,
    agents: dict | None = None,
) -> TurnState:
    configure_tracing()
    if agents is not None and llm.budget_exhausted():
        _budget_notice()
        agents = None  # over today's spend cap: the rule-based agents run the turn
    graph = build_graph(gateway, agents)
    state = {"case_id": case_id, "trigger": trigger, "inbound": inbound, "inbound_channel": inbound_channel, "path": []}
    config = {
        "run_name": f"turn {case_id} {trigger}",
        "tags": [trigger],
        "metadata": {"case_id": case_id, "trigger": trigger},
    }
    return await graph.ainvoke(state, config=config)
