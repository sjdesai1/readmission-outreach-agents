"""The outreach graph: rule nodes + 5 agents, wired with LangGraph. One run = one turn for one patient.

load_context -> input_guard -> route
  outreach_due  -> care_planner -> scheduler -> message_writer
  inbound_reply -> reply_interpreter -> by intent:
        wants_visit / reschedule -> scheduler -> message_writer
        question / barrier       -> care_planner -> message_writer
        opt_out                  -> opt_out
        third_party              -> proxy_reply  (family member: privacy-safe template, no LLM)
        symptom / abusive        -> escalate
        unclear (2nd time)       -> escalate
message_writer -> output_checks -> safety_reviewer -> send      (one rewrite allowed, then escalate)
"""
import hashlib
from datetime import timedelta

from langgraph.graph import END, START, StateGraph

from readmit.agents.gateway import Gateway, ToolFailed
from readmit.agents.guards import check_draft, screen_inbound
from readmit.agents.state import MAX_REVISIONS, TurnState, parse_dt, reply_channel
from readmit.agents.stubs import STUBS

CLOSED = {"suppressed", "completed", "opted_out", "readmitted", "closed", "escalated"}
RECENT = 6                 # messages kept word for word; older ones become a one-line digest
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
    summary = await gw.call("patient_data", "get_patient_summary", case_id=cid)
    thread = await gw.call("messaging", "get_thread", case_id=cid, limit=50) or []
    older, recent = thread[:-RECENT], thread[-RECENT:]
    history = ""
    if older:
        n_out = sum(m["direction"] == "outbound" for m in older)
        history = (f"{len(older)} earlier messages ({n_out} from us, {len(older) - n_out} from the patient), "
                   f"from {older[0]['at'][:10]} to {older[-1]['at'][:10]}")
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
    return {"now": now, "case": case, "summary": summary, "thread": recent, "history": history,
            "unclear_streak": streak, "turn_key": turn_key, "skip": None, "revisions": 0}


async def input_guard(state: TurnState, gw: Gateway) -> dict:
    g = screen_inbound(state.get("inbound"))
    out = {"guard": g}
    if g["action"] == "emergency":
        out |= {"escalate_reason": f"possible emergency in patient reply: {state.get('inbound')!r}", "urgent": True}
    return out


async def output_checks(state: TurnState, gw: Gateway) -> dict:
    sched = state.get("schedule") or {}
    stay = (state.get("summary") or {}).get("stay") or {}
    problems = check_draft(state["draft"]["body"], reply_channel(state), sched.get("offered") or [],
                           sched.get("appointment"), private_terms=[stay.get("diagnosis_group")])
    if problems:
        return {"problems": problems, "revisions": state.get("revisions", 0) + 1}
    return {"problems": []}


async def send(state: TurnState, gw: Gateway) -> dict:
    cid, channel, draft = state["case_id"], reply_channel(state), state["draft"]
    intent = (state.get("intent") or {}).get("intent")
    try:
        msg = await gw.call("messaging", "send_message", case_id=cid, channel=channel, body=draft["body"],
                            subject=draft.get("subject"), idempotency_key=f"{state['turn_key']}:msg")
        if intent:
            await gw.call("cases", "add_case_note", case_id=cid, note=f"intent: {intent}")
        if intent == "decline":
            await gw.call("cases", "update_case_status", case_id=cid, status="closed", reason="patient declined")
        elif not (state.get("schedule") or {}).get("appointment"):
            nxt = (parse_dt(state["now"]) + FOLLOW_UP_AFTER).isoformat(timespec="minutes")
            await gw.call("cases", "update_case_status", case_id=cid, status="awaiting_reply",
                          reason=f"message sent by {channel}", next_action_at=nxt)
    except ToolFailed as e:
        return {"escalate_reason": f"send failed: {e.message}"}
    return {"outcome": {"action": "sent", "channel": channel, "message_id": msg["message_id"]}}


async def escalate(state: TurnState, gw: Gateway) -> dict:
    cid = state["case_id"]
    reason = state.get("escalate_reason") or _why_escalate(state)
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    sent = None
    if state.get("urgent") and state.get("inbound_channel"):
        try:
            sent = await gw.call("messaging", "send_message", case_id=cid, channel=state["inbound_channel"],
                                 body=EMERGENCY_MSG.get(lang, EMERGENCY_MSG["English"]),
                                 idempotency_key=f"{state['turn_key']}:safety")
        except ToolFailed:
            pass        # the nurse still gets the case; never let a failed send hide an emergency
    await gw.call("cases", "add_case_note", case_id=cid, note=f"escalated: {reason}")
    await gw.call("cases", "update_case_status", case_id=cid, status="escalated", reason=reason[:300])
    return {"escalate_reason": reason,
            "outcome": {"action": "escalated", "urgent": bool(state.get("urgent")), "reason": reason,
                        "safety_message_id": sent["message_id"] if sent else None}}


async def opt_out(state: TurnState, gw: Gateway) -> dict:
    cid = state["case_id"]
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    channel = state.get("inbound_channel")
    if channel:     # one confirmation, sent before the case closes (closed cases can't be messaged)
        try:
            await gw.call("messaging", "send_message", case_id=cid, channel=channel,
                          body=OPT_OUT_MSG.get(lang, OPT_OUT_MSG["English"]),
                          idempotency_key=f"{state['turn_key']}:optout")
        except ToolFailed:
            pass
    await gw.call("cases", "update_case_status", case_id=cid, status="opted_out", reason="patient opted out")
    return {"outcome": {"action": "opted_out"}}


async def proxy_reply(state: TurnState, gw: Gateway) -> dict:
    """Someone other than the patient replied. Fixed template: no name, no health details, no booking."""
    cid = state["case_id"]
    lang = (state.get("summary") or {}).get("patient", {}).get("preferred_language", "English")
    try:
        msg = await gw.call("messaging", "send_message", case_id=cid, channel=state["inbound_channel"],
                            body=PROXY_MSG.get(lang, PROXY_MSG["English"]),
                            idempotency_key=f"{state['turn_key']}:proxy")
    except ToolFailed as e:
        return {"escalate_reason": f"third-party reply, and the privacy reply failed: {e.message}"}
    await gw.call("cases", "add_case_note", case_id=cid, note="third party replied; no details shared")
    nxt = (parse_dt(state["now"]) + FOLLOW_UP_AFTER).isoformat(timespec="minutes")
    await gw.call("cases", "update_case_status", case_id=cid, status="awaiting_reply",
                  reason="third party replied; waiting for the patient", next_action_at=nxt)
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
    return {"opt_out": "opt_out", "emergency": "escalate", "third_party": "proxy_reply"}.get(
        state["guard"]["action"], "reply_interpreter")


def after_interpreter(state: TurnState) -> str:
    intent = state["intent"]["intent"]
    if intent in ("wants_visit", "reschedule"):
        return "scheduler"
    if intent in ("question", "barrier"):
        return "care_planner"
    if intent == "opt_out":
        return "opt_out"
    if intent == "third_party":
        return "proxy_reply"
    if intent in ("symptom", "abusive"):
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
    rules = {"load_context": load_context, "input_guard": input_guard, "output_checks": output_checks,
             "send": send, "escalate": escalate, "opt_out": opt_out, "proxy_reply": proxy_reply}
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
    g.add_conditional_edges("input_guard", after_guard, ["opt_out", "escalate", "proxy_reply", "reply_interpreter"])
    g.add_conditional_edges("reply_interpreter", after_interpreter,
                            ["scheduler", "care_planner", "opt_out", "proxy_reply", "escalate", "message_writer"])
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


async def run_turn(gateway: Gateway, case_id: str, trigger: str, inbound: str | None = None,
                   inbound_channel: str | None = None, agents: dict | None = None) -> TurnState:
    graph = build_graph(gateway, agents)
    state = {"case_id": case_id, "trigger": trigger, "inbound": inbound, "inbound_channel": inbound_channel,
             "path": []}
    config = {"run_name": f"turn {case_id} {trigger}", "tags": [trigger],
              "metadata": {"case_id": case_id, "trigger": trigger}}
    return await graph.ainvoke(state, config=config)
