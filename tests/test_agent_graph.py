"""The whole outreach graph, end to end, with stub agents (no LLM) and the real MCP servers in memory.

Fixture patients: C1 Maria (English, email only, telehealth ok), C2 Jose (Spanish, portal/sms/phone, prefers sms).
"""
import asyncio
from datetime import datetime

import pytest

from readmit import db
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.agents.state import when
from readmit.outreach import store

pytestmark = pytest.mark.usefixtures("pg")


def turn(case_id, reply=None, channel="sms", agents=None):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None, agents))


def outbound(case_id):
    return [m for m in store.get_thread(case_id) if m["direction"] == "outbound"]


def test_first_outreach_offers_real_slots_and_waits_for_a_reply():
    s = turn("C1")
    assert s["path"] == ["load_context", "care_planner", "scheduler", "message_writer", "output_checks",
                         "safety_reviewer", "send"]
    [msg] = outbound("C1")
    assert msg["channel"] == "email" and when("2026-09-10T09:00") in msg["body"]
    assert store.get_case("C1")["status"] == "awaiting_reply"


def test_rerunning_a_turn_never_sends_twice():
    turn("C1")
    turn("C1")
    assert len(outbound("C1")) == 1


def test_yes_books_the_slot_and_confirms():
    turn("C2")
    s = turn("C2", "yes, the first one works")
    assert s["schedule"]["appointment"]["status"] == "booked"
    assert store.get_case("C2")["status"] == "booked"
    assert "booked" in outbound("C2")[-1]["body"]


def test_stop_closes_the_case_without_any_agent():
    s = turn("C2", "STOP")
    assert s["path"] == ["load_context", "input_guard", "opt_out"]
    assert store.get_case("C2")["status"] == "opted_out"
    assert outbound("C2")[-1]["body"].startswith("No recibirá")          # Spanish confirmation


def test_emergency_skips_the_agents_and_alerts_a_nurse():
    s = turn("C2", "tengo dolor de pecho")
    assert s["path"] == ["load_context", "input_guard", "escalate"]
    assert s["outcome"]["urgent"] and store.get_case("C2")["status"] == "escalated"
    assert "911" in outbound("C2")[-1]["body"]


def test_invented_time_gets_one_rewrite_then_a_nurse():
    async def bad_writer(state, gw):
        return {"draft": {"subject": "x", "body": "See you Fri Sep 25, 4:00 PM!"}}
    s = turn("C1", agents={"message_writer": bad_writer})
    assert s["path"].count("message_writer") == 2 and s["path"][-1] == "escalate"
    assert outbound("C1") == [] and store.get_case("C1")["status"] == "escalated"


def test_writer_fixes_its_draft_after_feedback():
    async def writer(state, gw):
        if state.get("problems"):
            first = state["schedule"]["offered"][0]["when"]
            return {"draft": {"subject": "x", "body": f"Hi Maria, open time: {first}"}}
        return {"draft": {"subject": "x", "body": "See you Fri Sep 25, 4:00 PM!"}}
    s = turn("C1", agents={"message_writer": writer})
    assert s["outcome"]["action"] == "sent" and s["path"].count("message_writer") == 2


def test_second_unclear_reply_goes_to_a_nurse():
    turn("C2")
    assert turn("C2", "hmm")["outcome"]["action"] == "sent"              # first time: ask again
    assert turn("C2", "blue")["outcome"]["action"] == "escalated"        # second time: a human takes over


def test_case_not_open_yet_is_skipped():
    store.set_now(datetime(2026, 9, 1))
    s = turn("C1")
    assert s["path"] == ["load_context"] and s["skip"] == "case not open yet"


def test_each_role_only_reaches_its_own_tools():
    gw = Gateway.in_memory()
    with pytest.raises(PermissionError):
        asyncio.run(gw.scoped("message_writer").call("messaging", "send_message", case_id="C1", channel="email",
                                                     body="hi", idempotency_key="k"))
    with pytest.raises(PermissionError):
        asyncio.run(gw.call("patient_data", "get_patient_summary", case_id="C1"))


def test_family_member_gets_a_privacy_reply_and_nothing_else():
    turn("C1")
    s = turn("C1", "This is her daughter. What was she in the hospital for?", channel="email")
    assert s["path"] == ["load_context", "input_guard", "proxy_reply"]
    reply = outbound("C1")[-1]["body"]
    assert "Heart failure".lower() not in reply.lower() and "Maria" not in reply
    assert store.get_case("C1")["status"] == "awaiting_reply"                # still waiting for Maria herself


def test_a_draft_naming_the_diagnosis_is_rewritten():
    async def leaky(state, gw):
        if state.get("problems"):
            return {"draft": {"subject": "x", "body": "Hi Maria, checking in after your stay."}}
        return {"draft": {"subject": "x", "body": "Hi Maria, checking in about your heart failure."}}
    s = turn("C1", agents={"message_writer": leaky})
    assert s["outcome"]["action"] == "sent" and "heart failure" not in outbound("C1")[-1]["body"].lower()


def test_outreach_at_night_is_held_until_morning():
    store.set_now(datetime(2026, 9, 9, 21, 30))
    s = turn("C1")
    assert s["outcome"]["action"] == "scheduled" and s["outcome"]["deliver_at"].startswith("2026-09-10 08:00")


def test_phone_reply_needs_identity_before_anything_else():
    s = turn("C2", "yes the first one", channel="phone")
    assert s["path"] == ["load_context", "input_guard", "identity_check"]
    assert "año de nacimiento" in outbound("C2")[-1]["body"]               # asked, in Spanish
    assert store.get_case("C2")["status"] != "booked"


def test_right_birth_year_verifies_and_the_turn_continues():
    s = turn("C2", "1968, yes the first one", channel="phone")
    assert "reply_interpreter" in s["path"] and s["schedule"]["appointment"]["status"] == "booked"
    assert store.get_case("C2")["identity_verified_at"]


def test_three_wrong_answers_go_to_a_nurse():
    for year in ("1950", "1951"):
        turn("C2", f"it's {year}", channel="phone")
    s = turn("C2", "1952", channel="phone")
    assert s["outcome"]["action"] == "escalated" and "3 times" in s["outcome"]["reason"]


def test_call_scripts_end_with_the_identity_question():
    with db.transaction() as c:
        db.rows(c, "update sim.patients set preferred_channel = 'phone' where patient_id = 'P2'")
    turn("C2")
    assert outbound("C2")[-1]["channel"] == "phone"
    assert outbound("C2")[-1]["body"].endswith("¿podría decirme su año de nacimiento?")
