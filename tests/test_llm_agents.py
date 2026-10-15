"""The LLM agents' glue code, with a fake LLM: no API calls, no cost.

What's tested is everything around the model: the right prompt goes in, the answer is validated and checked,
the rules override the model where they must, and failures degrade safely.
"""

import asyncio
import json
from datetime import datetime

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
    answers["review_message"] = APPROVE  # tests that care about the reviewer replace this
    return answers, prompts


APPROVE = {"verdict": "approve", "reasons": [], "checks": {}}


PLAN = {
    "channel": "email",
    "prefer_telehealth": True,
    "approach": "Short email.",
    "talking_points": [],
    "referrals": [{"type": "transport", "reason": "Notes say no car."}],
}


def offer(user):
    return {
        "subject": "Your follow-up visit",
        "body": f"Hi {{first_name}}, could you come in {user['offered_slots'][0]['when']}?",
    }


def turn(case_id, reply=None, channel="email"):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(
        run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None, agents=llm_agents.LLM_AGENTS)
    )


def test_full_outreach_turn_with_llm_agents(fake_llm):
    answers, prompts = fake_llm
    answers |= {"plan_care": PLAN, "write_message": offer}
    s = turn("C1")
    assert s["outcome"]["action"] == "sent"
    sent = store.get_thread("C1")[-1]
    assert sent["body"].startswith("Hi Maria,")  # name filled in at send time
    assert [p["task"] for p in prompts] == ["plan_care", "write_message", "review_message"]
    assert all("Maria" not in p["user"] for p in prompts)  # the LLM never saw the name
    assert "Maria" not in json.dumps(s, default=str)  # nor did the state or the traces
    assert [e["type"] for e in store.get_case("C1")["recent_events"]][:1] == ["referral_requested"]


def test_planner_cannot_pick_a_channel_without_consent(fake_llm):
    answers, _ = fake_llm
    answers |= {"plan_care": {**PLAN, "channel": "sms", "referrals": []}, "write_message": offer}
    s = turn("C1")  # Maria only consented to email
    assert s["plan"]["channel"] == "email" and s["plan"]["channel_overridden"]


def test_low_confidence_reply_becomes_unclear(fake_llm):
    answers, _ = fake_llm
    answers |= {
        "interpret_reply": {"intent": "wants_visit", "accepts_offer": True, "confidence": 0.3},
        "write_message": {"subject": None, "body": "Sorry {first_name}, could you say that another way?"},
    }
    s = turn("C1", "mmm maybe")
    assert s["intent"]["intent"] == "unclear" and not s["intent"]["accepts"]


def test_clinical_question_goes_to_a_nurse_not_the_writer(fake_llm):
    answers, prompts = fake_llm
    answers |= {
        "interpret_reply": {"intent": "clinical_question", "question": "ibuprofen with my meds?", "confidence": 0.95}
    }
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


def test_rule_based_fallback_never_books_a_time_the_patient_ruled_out(fake_llm):
    answers, _ = fake_llm
    answers["schedule_step"] = LLMError("down")  # scheduler agent unavailable: the rule-based path runs
    answers |= {
        "interpret_reply": {
            "intent": "wants_visit",
            "accepts_offer": True,
            "preferred_days": ["Thursday"],
            "earliest_time": "14:00",
            "confidence": 0.9,
        },
        "write_message": offer,
    }
    s = turn("C1", "cant do 9, anything thursday after 2?")  # fixture slots are all at 9:00
    assert s["schedule"]["appointment"] is None and s["schedule"]["note"]
    assert store.get_case("C1")["status"] != "booked"


def test_rule_based_fallback_books_when_the_choice_fits(fake_llm):
    answers, _ = fake_llm
    answers["schedule_step"] = LLMError("down")
    answers |= {
        "interpret_reply": {
            "intent": "wants_visit",
            "accepts_offer": True,
            "preferred_days": ["Friday"],
            "part_of_day": "morning",
            "choice": "friday morning",
            "confidence": 0.9,
        },
        "write_message": lambda u: {"subject": "Booked", "body": f"You're booked for {u['appointment']['when']}."},
    }
    s = turn("C1", "friday morning works")
    assert s["schedule"]["appointment"]["when"].startswith("Fri Sep 11")


def test_patient_switches_channel_and_gets_the_answer_there(fake_llm):
    answers, _ = fake_llm
    answers |= {
        "interpret_reply": {"intent": "channel_change", "new_channel": "portal", "confidence": 0.9},
        "write_message": {"subject": "Got it", "body": "Got it {first_name}, the portal it is."},
    }
    s = turn("C2", "can you message me in the portal instead", channel="sms")
    assert "apply_channel_change" in s["path"] and s["outcome"]["channel"] == "portal"
    assert store.patient_summary("C2")["contact"]["preferred_channel"] == "portal"


def test_channel_change_without_contact_details_is_explained(fake_llm):
    answers, _ = fake_llm
    answers |= {
        "interpret_reply": {"intent": "channel_change", "new_channel": "sms", "confidence": 0.9},
        "write_message": {"subject": "Sorry", "body": "Sorry {first_name}, a nurse will help with that."},
    }
    s = turn("C1", "text me instead", channel="email")  # no mobile number on file
    assert not s["channel_change"]["ok"] and s["outcome"]["channel"] == "email"


# ------------------------------------------------------------------ scheduler agent
def wants(**details):
    return {"intent": "wants_visit", "accepts_offer": True, "confidence": 0.9, **details}


def steps(*plan):
    """A scripted scheduler: returns plan[i] at step i (each item may use the results seen so far)."""

    def next_action(user):
        done = user["steps_so_far"]
        item = plan[min(len(done), len(plan) - 1)]
        return item(done) if callable(item) else item

    return next_action


def found(done):
    return next(s["result"] for s in done if s["tool"] == "find_slots" and isinstance(s["result"], list))


def test_scheduler_agent_books_the_option_the_patient_picked(fake_llm):
    answers, prompts = fake_llm
    answers |= {
        "interpret_reply": wants(choice="the second one"),
        "schedule_step": steps(
            {"action": "call_tool", "tool": "find_slots", "args": {"limit": 3}},
            lambda done: {
                "action": "call_tool",
                "tool": "book_appointment",
                "args": {"slot_id": found(done)[1]["slot_id"]},
            },
        ),
        "write_message": lambda u: {"subject": "Booked", "body": f"You're booked for {u['appointment']['when']}."},
    }
    s = turn("C1", "the second one please")
    assert s["schedule"]["appointment"]["start_at"].startswith("2026-09-11")  # S2, not the first slot
    tools = json.loads(next(p for p in prompts if p["task"] == "schedule_step")["user"])["tools"]
    assert all("case_id" not in t["parameters"]["properties"] for t in tools)  # the model can't pick the patient


def test_scheduler_cannot_book_a_slot_it_never_saw(fake_llm):
    answers, _ = fake_llm
    answers |= {
        "interpret_reply": wants(),
        "schedule_step": steps(
            {"action": "call_tool", "tool": "book_appointment", "args": {"slot_id": "S999"}},
            {"action": "call_tool", "tool": "find_slots", "args": {}},
            lambda done: {"action": "offer", "slot_ids": [x["slot_id"] for x in found(done)[:2]]},
        ),
        "write_message": offer,
    }
    s = turn("C1", "yes")
    assert s["schedule"]["appointment"] is None and len(s["schedule"]["offered"]) == 2
    assert "not returned by find_slots" in s["schedule"]["agent_steps"][0]["result"]["error"]


def test_scheduler_cannot_book_a_time_the_patient_ruled_out(fake_llm):
    answers, _ = fake_llm
    answers |= {
        "interpret_reply": wants(earliest_time="14:00"),  # fixture slots are all at 9:00
        "schedule_step": steps(
            {"action": "call_tool", "tool": "find_slots", "args": {}},
            lambda done: {
                "action": "call_tool",
                "tool": "book_appointment",
                "args": {"slot_id": found(done)[0]["slot_id"]},
            },
            {"action": "give_up", "note": "nothing after 2 PM"},
        ),
        "write_message": offer,
    }
    s = turn("C1", "yes but only after 2")
    assert s["schedule"]["appointment"] is None and store.get_case("C1")["status"] != "booked"
    assert "contradicts what the patient asked" in s["schedule"]["agent_steps"][1]["result"]["error"]


def test_scheduler_moves_an_existing_appointment(fake_llm):
    answers, _ = fake_llm
    store.book_appointment("C1", "S1", "first-booking")  # Thu Sep 10
    answers |= {
        "interpret_reply": {
            "intent": "reschedule",
            "accepts_offer": True,
            "preferred_days": ["Friday"],
            "confidence": 0.9,
        },
        "schedule_step": steps(
            {"action": "call_tool", "tool": "find_slots", "args": {}},
            lambda done: {
                "action": "call_tool",
                "tool": "reschedule_appointment",
                "args": {"new_slot_id": next(x["slot_id"] for x in found(done) if "Fri" in x["when"])},
            },
        ),
        "write_message": lambda u: {"subject": "Moved", "body": f"You're booked for {u['appointment']['when']}."},
    }
    s = turn("C1", "can we move it to friday")
    assert s["schedule"]["appointment"]["start_at"].startswith("2026-09-11")


def test_first_outreach_needs_no_scheduler_llm_call(fake_llm):
    answers, prompts = fake_llm
    answers |= {"plan_care": {**PLAN, "referrals": []}, "write_message": offer}
    turn("C1")
    assert "schedule_step" not in [p["task"] for p in prompts]


def test_scheduler_llm_down_falls_back_to_rules(fake_llm):
    answers, _ = fake_llm
    answers |= {"interpret_reply": wants(), "schedule_step": LLMError("down"), "write_message": offer}
    s = turn("C1", "yes")
    assert s["schedule"]["appointment"]["status"] == "booked"


# ------------------------------------------------------------------ safety reviewer
def test_reviewer_sends_a_draft_back_with_reasons(fake_llm):
    answers, prompts = fake_llm
    answers["plan_care"] = PLAN
    seen = []

    def writer(user):
        seen.append(user["checker_feedback"])
        return offer(user)

    def review(user):
        if len([p for p in prompts if p["task"] == "review_message"]) == 1:
            return {"verdict": "revise", "reasons": ["Sounds cold; add a warm opening."], "checks": {}}
        return APPROVE

    answers["write_message"], answers["review_message"] = writer, review
    s = turn("C1")
    assert s["outcome"]["action"] == "sent" and s["path"].count("message_writer") == 2
    assert seen[1] == ["Sounds cold; add a warm opening."]


def test_reviewer_checks_overrule_its_own_approve(fake_llm):
    answers, _ = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    answers["review_message"] = {"verdict": "approve", "reasons": [], "checks": {"respectful_tone": False}}
    s = turn("C1")
    assert s["path"].count("message_writer") == 2  # treated as revise


def test_medical_advice_goes_to_a_nurse_not_a_rewrite(fake_llm):
    answers, _ = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    answers["review_message"] = {
        "verdict": "revise",
        "reasons": ["Tells the patient to keep taking their pills."],
        "checks": {"no_medical_advice": False},
    }
    s = turn("C1")
    assert s["outcome"]["action"] == "escalated" and s["path"].count("message_writer") == 1
    assert "safety reviewer" in s["outcome"]["reason"]


def test_reviewer_down_still_sends_after_the_rule_checks(fake_llm):
    answers, _ = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    answers["review_message"] = LLMError("timeout")
    s = turn("C1")
    assert s["outcome"]["action"] == "sent" and s["review"]["unavailable"]


# ------------------------------------------------------------------ patient memory
def test_standing_preference_is_remembered_and_used_later(fake_llm, add_slot):
    answers, prompts = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    turn("C1")
    answers["interpret_reply"] = {
        "intent": "wants_visit",
        "part_of_day": "afternoon",
        "standing": True,
        "confidence": 0.9,
    }
    answers["schedule_step"] = LLMError("down")  # rule-based scheduler: deterministic for the test
    add_slot("S6", datetime(2026, 9, 10, 14, 0))
    s = turn("C1", "I work mornings, so afternoons are always better")
    assert s["remembered"] == ["part_of_day"]
    assert all(int(x["start_at"][11:13]) >= 12 for x in s["schedule"]["offered"])
    assert store.patient_summary("C1")["memory"] == {"part_of_day": "afternoon"}
    writer_input = json.loads([p for p in prompts if p["task"] == "write_message"][-1]["user"])
    assert writer_input["remembered_this_turn"] == ["part_of_day"]


def test_one_off_request_is_not_remembered(fake_llm):
    answers, _ = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    turn("C1")
    answers["interpret_reply"] = {"intent": "wants_visit", "preferred_days": ["Thursday"], "confidence": 0.9}
    answers["schedule_step"] = LLMError("down")
    s = turn("C1", "Thursday works this week")
    assert not s.get("remembered") and store.patient_summary("C1")["memory"] == {}


def test_symptoms_are_never_remembered_as_preferences(fake_llm):
    answers, _ = fake_llm
    answers["interpret_reply"] = {"intent": "symptom", "part_of_day": "morning", "standing": True, "confidence": 0.9}
    s = turn("C1", "my ankles feel puffy every morning")
    assert "remember" in s["path"] and not s.get("remembered")


def test_a_preference_already_remembered_is_not_saved_or_announced_again(fake_llm):
    answers, _ = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    answers["schedule_step"] = LLMError("down")
    store.remember_preference("C1", "part_of_day", "afternoon", "I work mornings")
    turn("C1")
    answers["interpret_reply"] = {
        "intent": "wants_visit",
        "part_of_day": "afternoon",
        "mode": "in_person",
        "standing": True,
        "confidence": 0.9,
    }
    s = turn("C1", "Do you have clinic visits instead? Afternoons, like I said.")
    assert s["remembered"] == ["mode"]  # only what's new


def test_reviewer_sees_doctors_and_the_conversation(fake_llm):
    answers, prompts = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    turn("C1")
    review = json.loads(next(p for p in prompts if p["task"] == "review_message")["user"])
    assert review["facts"]["offered_slots"][0]["with"] == "Dr. Test"
    assert "conversation" in review and "remembered_this_turn" in review["facts"]


def test_reviewer_escalate_without_medical_advice_gets_one_rewrite_first(fake_llm):
    answers, _ = fake_llm
    answers["plan_care"], answers["write_message"] = PLAN, offer
    answers["review_message"] = {"verdict": "escalate", "reasons": ["Promises something not in the facts."]}
    s = turn("C1")
    assert s["path"].count("message_writer") == 2 and s["outcome"]["action"] == "escalated"
