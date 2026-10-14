"""The LLM agents' glue code, with a fake LLM: no API calls, no cost.

What's tested is everything around the model: the right prompt goes in, the answer is validated and checked,
the rules override the model where they must, and failures degrade safely.
"""
import asyncio
import json

import pytest

from readmit.agents import llm_agents
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.llm import LLMError
from readmit.outreach import store

pytestmark = pytest.mark.usefixtures("pg")


@pytest.fixture
def fake_llm(monkeypatch):
    """Canned answers per task; records every prompt sent."""
    answers, prompts = {}, []

    def complete_json(task, prompt_version, system, user, schema, **kw):
        prompts.append({"task": task, "version": prompt_version, "user": user})
        a = answers[task]
        if isinstance(a, Exception):
            raise a
        return schema.model_validate(a(json.loads(user)) if callable(a) else a), None
    monkeypatch.setattr(llm_agents, "complete_json", complete_json)
    return answers, prompts


PLAN = {"channel": "email", "prefer_telehealth": True, "approach": "Short email.", "talking_points": [],
        "referrals": [{"type": "transport", "reason": "Notes say no car."}]}


def offer(user):
    return {"subject": "Your follow-up visit",
            "body": f"Hi {{first_name}}, could you come in {user['offered_slots'][0]['when']}?"}


def turn(case_id, reply=None, channel="email"):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None,
                                agents=llm_agents.LLM_AGENTS))


def test_full_outreach_turn_with_llm_agents(fake_llm):
    answers, prompts = fake_llm
    answers |= {"plan_care": PLAN, "write_message": offer}
    s = turn("C1")
    assert s["outcome"]["action"] == "sent"
    sent = store.get_thread("C1")[-1]
    assert sent["body"].startswith("Hi Maria,")                         # name filled in at send time
    assert [p["task"] for p in prompts] == ["plan_care", "write_message"]
    assert all("Maria" not in p["user"] for p in prompts)                # the LLM never saw the name
    assert "Maria" not in json.dumps(s, default=str)                     # nor did the state or the traces
    assert [e["type"] for e in store.get_case("C1")["recent_events"]][:1] == ["referral_requested"]


def test_planner_cannot_pick_a_channel_without_consent(fake_llm):
    answers, _ = fake_llm
    answers |= {"plan_care": {**PLAN, "channel": "sms", "referrals": []}, "write_message": offer}
    s = turn("C1")                                                       # Maria only consented to email
    assert s["plan"]["channel"] == "email" and s["plan"]["channel_overridden"]


def test_low_confidence_reply_becomes_unclear(fake_llm):
    answers, _ = fake_llm
    answers |= {"interpret_reply": {"intent": "wants_visit", "accepts_offer": True, "confidence": 0.3},
                "write_message": {"subject": None, "body": "Sorry {first_name}, could you say that another way?"}}
    s = turn("C1", "mmm maybe")
    assert s["intent"]["intent"] == "unclear" and not s["intent"]["accepts"]


def test_clinical_question_goes_to_a_nurse_not_the_writer(fake_llm):
    answers, prompts = fake_llm
    answers |= {"interpret_reply": {"intent": "clinical_question", "question": "ibuprofen with my meds?",
                                    "confidence": 0.95}}
    s = turn("C1", "can I take ibuprofen with my new pills?")
    assert s["path"][-1] == "escalate" and "write_message" not in [p["task"] for p in prompts]


def test_llm_down_while_reading_a_reply_escalates(fake_llm):
    answers, _ = fake_llm
    answers["interpret_reply"] = LLMError("both models failed")
    s = turn("C1", "yes please")
    assert s["outcome"]["action"] == "escalated" and "LLM unavailable" in s["outcome"]["reason"]


def test_llm_down_while_writing_falls_back_to_a_template(fake_llm):
    answers, _ = fake_llm
    answers |= {"plan_care": LLMError("down"), "write_message": LLMError("down")}
    s = turn("C1")
    assert s["outcome"]["action"] == "sent" and store.get_thread("C1")[-1]["body"].startswith("Hi Maria")


def test_writer_gets_checker_feedback_on_rewrite(fake_llm):
    answers, prompts = fake_llm

    def writer(user):
        if user["checker_feedback"]:
            return offer(user)
        return {"subject": "x", "body": "Hi {first_name}, URGENT: book now!!"}
    answers |= {"plan_care": {**PLAN, "referrals": []}, "write_message": writer}
    s = turn("C1")
    assert s["outcome"]["action"] == "sent"
    rewrite = json.loads([p for p in prompts if p["task"] == "write_message"][1]["user"])
    assert any("ALL CAPS" in f for f in rewrite["checker_feedback"])
