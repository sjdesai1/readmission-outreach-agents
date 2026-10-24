"""Bugs found by reading and labeling real messages (milestone 6), each pinned by a test (ADR 0011).

Fixture patients: C1 Maria (English, email, telehealth ok, no ride on file), C2 Jose (Spanish, sms, no telehealth).
"""

import asyncio

import pytest

from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.outreach import store

pytestmark = pytest.mark.usefixtures("pg")


def turn(case_id, reply=None, channel="sms", agents=None):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None, agents))


def last(case_id):
    return [m for m in store.get_thread(case_id) if m["direction"] == "outbound"][-1]["body"]


def test_leg_swelling_inside_a_yes_goes_to_a_nurse_not_a_booking():
    turn("C1")
    s = turn("C1", "Yes the first one is fine. My legs have been a bit more puffy this week though.", "email")
    assert s["outcome"]["nurse_task"]["priority"] == "urgent"
    assert not (s.get("schedule") or {}).get("appointment")


def test_a_patient_who_cant_do_video_is_told_so_when_booked_in_the_clinic():
    turn("C2")
    s = turn("C2", "A video visit would work. The first one is fine.")
    assert s["schedule"]["appointment"]["mode"] == "in_person" and s["schedule"]["video_unavailable"]
    assert "video" in last("C2") and "clínica" in last("C2")


def test_a_video_request_replaces_a_clinic_pick_with_video_times():
    turn("C1")
    s = turn("C1", "A video visit would work. The first one is fine.", "email")
    assert not s["schedule"]["appointment"]
    assert s["schedule"]["offered"] and all(o["mode"] == "telehealth" for o in s["schedule"]["offered"])


def test_a_clinic_booking_for_a_patient_without_a_ride_says_the_ride_is_arranged():
    turn("C1")  # Maria's transport barrier is on file: the first outreach files the ride referral
    turn("C1", "Yes, the first one works for me.", "email")
    assert "ride" in last("C1")


def test_a_cost_worry_gets_a_social_worker_not_a_yes_or_no():
    turn("C1")
    s = turn("C1", "I can't afford another copay this month.", "email")
    assert s["intent"]["intent"] == "barrier" and "social_work" in s["plan"]["referrals_filed"]
    assert "social worker" in last("C1")


def test_jose_with_no_ride_is_not_offered_video():
    turn("C2")
    turn("C2", "I don't have a ride to get there")
    assert "video" not in last("C2") and "transporte" in last("C2")


def test_the_rule_based_question_reply_promises_no_call_nobody_will_make():
    turn("C1")
    s = turn("C1", "Where is the clinic? Is there parking?", "email")
    body = last("C1").lower()
    assert s["outcome"]["action"] == "sent" and "nurse will" not in body and "reply nurse" in body
    assert not store.list_nurse_tasks()


def test_a_draft_promising_a_nurse_call_is_sent_back_and_then_escalated():
    async def promises_a_call(state, gw):
        return {"draft": {"subject": None, "body": "Hi {first_name}, a nurse will call you tomorrow about this."}}

    turn("C1")
    s = turn("C1", "Where is the clinic? Is there parking?", "email", agents={"message_writer": promises_a_call})
    assert s["path"].count("message_writer") == 2  # the check sent it back once
    assert s["outcome"]["action"] == "escalated" and "nurse task" in s["outcome"]["reason"]


def test_a_spanish_draft_with_tu_is_sent_back():
    async def informal(state, gw):
        return {"draft": {"subject": None, "body": "Hola {first_name}, ¿puedes decirnos qué hora te funciona?"}}

    turn("C2")
    s = turn("C2", "¿Cuánto dura la cita?", agents={"message_writer": informal})
    assert any("usted" in p for p in s["problems"])
