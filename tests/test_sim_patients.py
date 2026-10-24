"""Scripted patients: pure rules, no database."""

from datetime import datetime

from readmit.sim.patients import Patient, respond

T = datetime(2026, 9, 9, 9, 0)
OFFER = {"channel": "sms", "body": "Open times: 1) Thu Sep 10, 9:00 AM. Reply with the number."}


def test_replies_follow_the_script_then_only_pick_new_times():
    p = Patient("C1", "busy", 1953)
    first, second, rebook1, rebook2, done = (respond(p, OFFER, T) for _ in range(5))
    assert "afternoons" in first.text and "first one" in second.text
    assert rebook1.text == rebook2.text == "The first one works." and done is None  # rebooks at most twice
    assert respond(Patient("C2", "busy", 1960, replies_sent=2), {"channel": "sms", "body": "Any questions?"}, T) is None
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


def test_attendance_is_seeded_per_visit():
    from readmit.sim.patients import attends

    assert attends("busy", "C1|2026-09-10 09:00") == attends("busy", "C1|2026-09-10 09:00")
    assert sum(attends("no_ride", f"a{i}") for i in range(200)) < sum(attends("engaged", f"a{i}") for i in range(200))


def test_the_language_question_alone_gets_no_reply():
    p = Patient("C2", "med_question", 1968, replies_sent=1)  # script done; only new offers get an answer
    booked = "Hola Jose, su visita está reservada para el vie 11 sep, 9:30 AM. Would you prefer our messages in English? Reply ENGLISH."
    assert respond(p, {"channel": "sms", "body": booked}, T) is None and p.rebook_replies == 0
