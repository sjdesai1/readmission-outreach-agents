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
    assert s["path"] == [
        "load_context",
        "care_planner",
        "scheduler",
        "message_writer",
        "output_checks",
        "safety_reviewer",
        "send",
    ]
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
    assert outbound("C2")[-1]["body"].startswith("No recibirá")  # Spanish confirmation


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
    assert turn("C2", "hmm")["outcome"]["action"] == "sent"  # first time: ask again
    assert turn("C2", "blue")["outcome"]["action"] == "escalated"  # second time: a human takes over


def test_case_not_open_yet_is_skipped():
    store.set_now(datetime(2026, 9, 1))
    s = turn("C1")
    assert s["path"] == ["load_context"] and s["skip"] == "case not open yet"


def test_standing_preference_carries_over_to_the_next_outreach(add_slot):
    add_slot("S6", datetime(2026, 9, 10, 14, 0))
    store.remember_preference("C1", "part_of_day", "afternoon", "I work mornings")
    s = turn("C1")
    assert s["schedule"]["offered"] and all(int(x["start_at"][11:13]) >= 12 for x in s["schedule"]["offered"])


def test_each_role_only_reaches_its_own_tools():
    gw = Gateway.in_memory()
    with pytest.raises(PermissionError):
        asyncio.run(
            gw.scoped("message_writer").call(
                "messaging", "send_message", case_id="C1", channel="email", body="hi", idempotency_key="k"
            )
        )
    with pytest.raises(PermissionError):
        asyncio.run(gw.call("patient_data", "get_patient_summary", case_id="C1"))


def test_family_member_gets_a_privacy_reply_and_nothing_else():
    turn("C1")
    s = turn("C1", "This is her daughter. What was she in the hospital for?", channel="email")
    assert s["path"] == ["load_context", "input_guard", "proxy_reply"]
    reply = outbound("C1")[-1]["body"]
    assert "Heart failure".lower() not in reply.lower() and "Maria" not in reply
    assert store.get_case("C1")["status"] == "awaiting_reply"  # still waiting for Maria herself


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
    assert "año de nacimiento" in outbound("C2")[-1]["body"]  # asked, in Spanish
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


# ------------------------------------------------------------------ nurse handoff
def reads_as(intent):
    async def interpreter(state, gw):
        return {"intent": {"intent": intent, "accepts": False, "details": {}}}

    return {"reply_interpreter": interpreter}


def test_clinical_question_becomes_a_nurse_task_with_an_honest_time():
    turn("C1")  # Wed Sep 9: first outreach
    s = turn("C1", "Can I take ibuprofen with my new pills?", channel="email", agents=reads_as("clinical_question"))
    task = s["outcome"]["nurse_task"]
    assert task["kind"] == "clinical_question" and task["priority"] == "routine"
    assert task["due_at"].startswith("2026-09-10 17:00")  # next business day, 5 PM
    ack = outbound("C1")[-1]["body"]
    assert "by Thu Sep 10, 5:00 PM" in ack and "ibuprofen" not in ack  # promise matches the queue; no answer
    assert store.get_case("C1")["status"] == "escalated"


def test_symptom_is_urgent_and_the_ack_goes_out_even_at_night():
    store.set_now(datetime(2026, 9, 9, 22, 0))
    s = turn("C2", "mis tobillos están más hinchados", agents=reads_as("symptom"))
    assert s["outcome"]["nurse_task"]["priority"] == "urgent"
    assert s["outcome"]["nurse_task"]["due_at"].startswith("2026-09-09 23:00")
    ack = next(m for m in outbound("C2") if m["message_id"] == s["outcome"]["ack_message_id"])
    assert ack["status"] != "scheduled" and "próxima hora" in ack["body"]  # safety: not held for quiet hours


def test_the_ai_stays_quiet_until_a_nurse_hands_the_case_back():
    s = turn("C2", "can I talk to a nurse?", agents=reads_as("wants_human"))
    before = len(outbound("C2"))
    assert turn("C2", "hello?")["outcome"]["action"] == "held_for_nurse"  # the nurse owns the conversation now
    assert len(outbound("C2")) == before
    store.resolve_nurse_task(s["outcome"]["nurse_task"]["task_id"], "called, answered", hand_back=True)
    assert store.get_case("C2")["status"] == "awaiting_reply"
    assert turn("C2", "ok thanks, book me")["outcome"]["action"] == "sent"


def test_abusive_reply_gets_a_review_task_and_no_reply():
    before = len(outbound("C2"))
    s = turn("C2", "...", agents=reads_as("abusive"))
    assert s["outcome"]["nurse_task"]["kind"] == "review" and len(outbound("C2")) == before


def test_routine_callbacks_roll_to_the_next_day_when_the_nurses_are_full(monkeypatch):
    monkeypatch.setattr(store, "NURSE_DAILY_CAPACITY", 1)
    first = turn("C1", "call me please", channel="email", agents=reads_as("wants_human"))
    second = turn("C2", "llámenme por favor", agents=reads_as("wants_human"))
    assert first["outcome"]["nurse_task"]["due_at"].startswith("2026-09-10 17:00")
    assert second["outcome"]["nurse_task"]["due_at"].startswith("2026-09-11 17:00")
    assert "vie 11 sep, 5:00 PM" in outbound("C2")[-1]["body"]  # Spanish date, same honest time


def test_failed_draft_on_an_outreach_turn_sends_nothing_to_the_patient():
    async def bad_writer(state, gw):
        return {"draft": {"subject": "x", "body": "See you Fri Sep 25, 4:00 PM!"}}

    s = turn("C1", agents={"message_writer": bad_writer})
    assert s["outcome"]["nurse_task"]["kind"] == "review" and outbound("C1") == []


def test_a_question_about_visit_types_gets_real_slots_not_a_promise():
    async def asks(state, gw):
        return {"intent": {"intent": "question", "accepts": False, "details": {"mode": "in_person"}}}

    turn("C1")
    s = turn("C1", "Do you have clinic visits instead?", channel="email", agents={"reply_interpreter": asks})
    assert "scheduler" in s["path"] and s["schedule"]["offered"]


def test_an_emergency_while_a_nurse_has_the_case_still_gets_911():
    turn("C2", "can I talk to a nurse?", agents=reads_as("wants_human"))
    s = turn("C2", "ahora tengo dolor de pecho")
    assert s["path"] == ["load_context", "input_guard", "escalate"]
    assert "911" in outbound("C2")[-1]["body"] and s["outcome"]["nurse_task"]["priority"] == "urgent"


def test_paused_case_gets_no_outreach_turns():
    turn("C2", "can I talk to a nurse?", agents=reads_as("wants_human"))
    assert turn("C2")["skip"] == "case is escalated"


# ------------------------------------------------------------------ appointment lifecycle
def test_reminder_names_the_time_type_and_doctor_once():
    a = store.book_appointment("C1", "S2", "kr1")
    store.set_now(datetime(2026, 9, 10, 9, 0))
    for _ in range(2):
        s = asyncio.run(run_turn(Gateway.in_memory(), "C1", "reminder"))
    assert s["path"] == ["load_context", "remind"] and s["outcome"]["action"] == "reminded"
    [msg] = outbound("C1")
    assert "Fri Sep 11, 9:00 AM" in msg["body"] and "clinic visit" in msg["body"] and "Dr. Test" in msg["body"]
    assert "Maria" in msg["body"] and a["appointment_id"]


def test_no_show_gets_a_no_blame_rebooking_offer_and_a_fresh_cadence():
    turn("C1")
    turn("C1", "the first one works", channel="email")
    appt = store.upcoming_appointment("C1")
    store.set_now(datetime(2026, 9, 10, 10, 0))
    store.record_visit(appt["appointment_id"], attended=False)
    s = turn("C1")
    assert s["cadence"]["attempt"] == 1  # the offers before the no-show don't count
    assert "missed you" in outbound("C1")[-1]["body"]


def test_a_barrier_in_a_reply_is_recorded_and_gets_a_ride_referral():
    turn("C2")
    s = turn("C2", "I don't have a ride to get there")
    assert s["intent"]["intent"] == "barrier" and "transport_barrier" in s["summary"]["risk"]["barriers"]
    assert s["plan"]["referrals_filed"] == ["transport"]
    assert "ride" in outbound("C2")[-1]["body"]
