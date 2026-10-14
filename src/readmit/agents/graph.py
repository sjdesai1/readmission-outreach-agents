"""The outreach graph: rule nodes + 5 agents, wired with LangGraph. One run = one turn for one patient.

load_context -> input_guard -> route
  phone reply, identity not yet confirmed -> identity_check (year of birth, checked server-side) -> continue / ask
  outreach_due  -> care_planner -> scheduler -> message_writer
  inbound_reply -> reply_interpreter -> by intent:
        wants_visit / reschedule -> scheduler -> message_writer
        question / barrier       -> care_planner -> message_writer
        decline                  -> message_writer
        channel_change           -> apply_channel_change -> message_writer (answers on the new channel)
        clinical_question / wants_human / symptom / abusive -> escalate   (nurse handoff comes in step 5)
        opt_out                  -> opt_out
        third_party              -> proxy_reply  (family member: privacy-safe template, no LLM)
        symptom / abusive        -> escalate
        unclear (2nd time)       -> escalate
message_writer -> output_checks -> safety_reviewer -> send      (one rewrite allowed, then escalate)
"""

import hashlib
import re
from datetime import timedelta

from langgraph.graph import END, START, StateGraph

from readmit.agents.gateway import Gateway, ToolFailed
from readmit.agents.guards import check_draft, screen_inbound
from readmit.agents.privacy import configure_tracing, pseudonymize, pseudonymize_summary, render
from readmit.agents.state import MAX_REVISIONS, TurnState, parse_dt, reply_channel
from readmit.agents.stubs import STUBS

CLOSED = {"suppressed", "completed", "opted_out", "readmitted", "closed", "escalated"}
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
    if case["status"] in CLOSED:
        return {"now": now, "case": case, "skip": f"case is {case['status']}"}
    if parse_dt(case["opened_at"]) > parse_dt(now):
        return {"now": now, "case": case, "skip": "case not open yet"}
    if state["trigger"] == "outreach_due" and case["status"] == "booked":
        return {"now": now, "case": case, "skip": "already booked"}
    # the patient's name never enters the state: agents, prompts and traces see {first_name} instead
    summary, name = pseudonymize_summary(await gw.call("patient_data", "get_patient_summary", case_id=cid))
    thread = [
        {**m, "body": pseudonymize(m["body"], name)}
        for m in await gw.call("messaging", "get_thread", case_id=cid, limit=50) or []
    ]
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


async def escalate(state: TurnState, gw: Gateway) -> dict:
    cid = state["case_id"]
    reason = state.get("escalate_reason") or _why_escalate(state)
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    sent = None
    if state.get("urgent") and state.get("inbound_channel"):
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
        except ToolFailed:
            pass  # the nurse still gets the case; never let a failed send hide an emergency
    await gw.call("cases", "add_case_note", case_id=cid, note=f"escalated: {reason}")
    await gw.call("cases", "update_case_status", case_id=cid, status="escalated", reason=reason[:300])
    return {
        "escalate_reason": reason,
        "outcome": {
            "action": "escalated",
            "urgent": bool(state.get("urgent")),
            "reason": reason,
            "safety_message_id": sent["message_id"] if sent else None,
        },
    }


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
        return f"patient reply needs a nurse (intent: {intent})"
    if state.get("plan") and not state["plan"].get("channel"):
        return "no channel the patient consented to"
    return "unspecified"


# ------------------------------------------------------------------ routing (pure functions of state)
def after_context(state: TurnState) -> str:
    if state.get("skip"):
        return END
    return "input_guard" if state["trigger"] == "inbound_reply" else "care_planner"


def after_guard(state: TurnState) -> str:
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


def after_interpreter(state: TurnState) -> str:
    if state.get("escalate_reason"):
        return "escalate"
    intent = state["intent"]["intent"]
    if intent in ("wants_visit", "reschedule"):
        return "scheduler"
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
    g.add_conditional_edges("load_context", after_context, ["input_guard", "care_planner", END])
    g.add_conditional_edges(
        "input_guard", after_guard, ["opt_out", "escalate", "proxy_reply", "identity_check", "reply_interpreter"]
    )
    g.add_conditional_edges("identity_check", after_identity, ["reply_interpreter", "escalate", END])
    g.add_conditional_edges(
        "reply_interpreter",
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


async def run_turn(
    gateway: Gateway,
    case_id: str,
    trigger: str,
    inbound: str | None = None,
    inbound_channel: str | None = None,
    agents: dict | None = None,
) -> TurnState:
    configure_tracing()
    graph = build_graph(gateway, agents)
    state = {"case_id": case_id, "trigger": trigger, "inbound": inbound, "inbound_channel": inbound_channel, "path": []}
    config = {
        "run_name": f"turn {case_id} {trigger}",
        "tags": [trigger],
        "metadata": {"case_id": case_id, "trigger": trigger},
    }
    return await graph.ainvoke(state, config=config)
