"""Scripted patients: pure rules, no database."""

from datetime import datetime

from readmit.sim.patients import Patient, respond

T = datetime(2026, 9, 9, 9, 0)
OFFER = {"channel": "sms", "body": "Open times: 1) Thu Sep 10, 9:00 AM. Reply with the number."}


def test_replies_follow_the_script_then_stop():
    p = Patient("C1", "busy", 1953)
    first, second, third = (respond(p, OFFER, T) for _ in range(3))
    assert "afternoons" in first.text and "first one" in second.text and third is None
    assert 12 <= (first.due_at - T).total_seconds() / 3600 <= 30


def test_no_reply_to_messages_that_ask_nothing():
    p = Patient("C1", "engaged", 1953)
    assert respond(p, {"channel": "sms", "body": "You're booked for Thu Sep 10, 9:00 AM."}, T) is None
    assert respond(Patient("C2", "silent", 1960), OFFER, T) is None


def test_phone_patients_give_their_birth_year_when_asked():
    call = {"channel": "phone", "body": "... could you please tell me your year of birth?"}
    assert respond(Patient("C1", "engaged", 1953), call, T).text.startswith("1953. ")
    assert respond(Patient("C2", "opt_out", 1960), call, T).text == "STOP"  # STOP stays the whole message


def test_same_seed_same_timing():
    a = respond(Patient("C1", "engaged", 1953), OFFER, T)
    b = respond(Patient("C1", "engaged", 1953), OFFER, T)
    assert a.due_at == b.due_at
