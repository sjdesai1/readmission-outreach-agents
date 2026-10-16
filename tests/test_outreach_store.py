"""Integration tests: the outreach rules against a real Postgres (a separate `app_test` database).

Skipped automatically when Postgres isn't reachable. CI runs them with a Postgres service container.
"""

from datetime import datetime, timedelta

import pytest

from readmit import db
from readmit.outreach import store
from readmit.outreach.store import StoreError

pytestmark = pytest.mark.usefixtures("pg")


def test_summary_is_minimum_necessary():
    s = store.patient_summary("C1")
    assert s["patient"]["first_name"] == "Maria"
    assert "maria@example.com" not in str(s)  # agents never see contact details
    assert s["contact"]["allowed_channels"] == ["email"]
    assert s["risk"]["barriers"] == ["transport_barrier"]


def test_find_slots_skips_taken_too_soon_and_disallowed_telehealth():
    ids = [s["slot_id"] for s in store.find_slots("C2", limit=10)]  # P2 can't do telehealth
    assert ids == ["S1", "S2"]  # S3 telehealth, S4 under 2h away, S5 taken by someone else


def test_booking_is_idempotent_and_blocks_double_booking():
    first = store.book_appointment("C1", "S1", "key-1")
    again = store.book_appointment("C1", "S1", "key-1")
    assert again["appointment_id"] == first["appointment_id"]
    with pytest.raises(StoreError, match="already has appointment"):
        store.book_appointment("C1", "S2", "key-2")
    with pytest.raises(StoreError, match="no longer available"):
        store.book_appointment("C2", "S1", "key-3")


def test_telehealth_requires_consent():
    with pytest.raises(StoreError, match="telehealth"):
        store.book_appointment("C2", "S3", "key-4")


def test_reschedule_frees_the_old_slot():
    a = store.book_appointment("C1", "S1", "key-5")
    store.reschedule_appointment(a["appointment_id"], "S2", "key-6")
    ids = [s["slot_id"] for s in store.find_slots("C2", limit=10)]
    assert "S1" in ids and "S2" not in ids


def test_messages_respect_consent_and_idempotency():
    m1 = store.send_message("C1", "email", "Checking in", "msg-1")
    m2 = store.send_message("C1", "email", "Checking in", "msg-1")
    assert m1["message_id"] == m2["message_id"]
    assert len(store.get_thread("C1")) == 1
    with pytest.raises(StoreError, match="not opted into sms"):
        store.send_message("C1", "sms", "hi", "msg-2")


def test_closed_case_gets_no_messages():
    store.update_case("C2", "opted_out", "patient replied STOP")
    with pytest.raises(StoreError, match="opted_out"):
        store.send_message("C2", "sms", "hi", "msg-3")


def test_every_action_is_audited():
    store.book_appointment("C1", "S1", "key-7")
    store.create_referral("C1", "transport", "no car", "ref-1")
    types = [e["type"] for e in store.get_case("C1")["recent_events"]]
    assert types == ["appointment_booked", "referral_requested"]


def test_no_actions_before_the_case_opens():
    """Reproduces the Inspector bug: clock at Sep 1, case opens Sep 9, booking used to succeed."""
    store.set_now(datetime(2026, 9, 1))
    for action in (
        lambda: store.book_appointment("C1", "S1", "early-1"),
        lambda: store.send_message("C1", "email", "hi", "early-2"),
        lambda: store.create_referral("C1", "transport", "no car", "early-3"),
        lambda: store.update_case("C1", "contacted", "early"),
    ):
        with pytest.raises(StoreError, match="not open yet"):
            action()
    assert store.get_case("C1")["recent_events"] == []  # reads still work, nothing was written


NOW = datetime(2026, 9, 9, 9, 0)


def add_spanish_speaking_doctor():
    with db.transaction() as c:
        db.rows(
            c,
            """insert into outreach.providers (provider_id, name, role, languages)
                      values ('PRV09', 'Dra. Ruiz', 'primary care', '{English,Spanish}')""",
        )
        db.rows(
            c,
            """insert into outreach.slots (slot_id, provider_id, start_at, end_at, mode, status)
                      values ('S9', 'PRV09', :s, :e, 'in_person', 'open')""",
            s=NOW + timedelta(days=3),
            e=NOW + timedelta(days=3, minutes=30),
        )


def test_quiet_hours_hold_messages_until_morning():
    store.set_now(datetime(2026, 9, 9, 22, 0))
    m = store.send_message("C1", "email", "Checking in", "late-1")
    assert m["status"] == "scheduled" and m["deliver_at"].startswith("2026-09-10 08:00")
    assert store.deliver_due() == 0
    store.set_now(datetime(2026, 9, 10, 8, 5))
    assert store.deliver_due() == 1 and store.get_thread("C1")[-1]["status"] == "sent"


def test_safety_messages_ignore_quiet_hours():
    store.set_now(datetime(2026, 9, 9, 23, 30))
    assert store.send_message("C2", "sms", "Call 911 now", "s-1", priority="safety")["status"] == "sent"


def test_slots_rank_language_then_own_doctor():
    add_spanish_speaking_doctor()
    assert store.find_slots("C2", limit=3)[0]["slot_id"] == "S9"  # Jose speaks Spanish
    with db.transaction() as c:
        db.rows(c, "update outreach.cases set pcp_provider_id = 'PRV09' where case_id = 'C1'")
    first = store.find_slots("C1", limit=3)[0]  # Maria: her own doctor first
    assert first["slot_id"] == "S9" and first["own_doctor"]


def test_follow_up_deadline_beats_everything():
    add_spanish_speaking_doctor()
    with db.transaction() as c:
        db.rows(c, "update outreach.cases set follow_up_by = :d where case_id = 'C2'", d=NOW + timedelta(days=2))
    slots = store.find_slots("C2", limit=3)
    assert slots[0]["slot_id"] == "S1" and slots[-1]["slot_id"] == "S9" and not slots[-1]["within_target"]


def test_channel_change_needs_the_contact_on_file():
    with pytest.raises(StoreError, match="no sms contact"):
        store.set_preferred_channel("C1", "sms", "text me instead")  # Maria has no mobile number
    assert store.set_preferred_channel("C2", "portal", "use the portal")["preferred_channel"] == "portal"
    assert store.get_case("C2")["recent_events"][-1]["type"] == "consent_changed"


def test_identity_check_never_reveals_the_answer_and_locks():
    for left in (2, 1, 0):
        assert store.verify_identity("C1", 1960) == {"verified": False, "attempts_left": left}
    with pytest.raises(StoreError, match="locked"):
        store.verify_identity("C1", 1954)  # even the right answer, once locked
    assert store.verify_identity("C2", 1968) == {"verified": True}
    types = [e["type"] for e in store.get_case("C1")["recent_events"]]
    assert types == ["identity_failed", "identity_failed", "identity_locked"]


# ------------------------------------------------------------------ nurse handoff and memory
def test_nurse_queue_is_urgent_first_idempotent_and_hands_back():
    routine = store.create_nurse_task("C1", "callback", "routine", "wants a call", "k1")
    again = store.create_nurse_task("C1", "callback", "routine", "wants a call", "k1")
    assert again["task_id"] == routine["task_id"] and "already" in again["note"]
    urgent = store.create_nurse_task("C2", "symptom", "urgent", "swelling", "k2")
    assert [t["task_id"] for t in store.list_nurse_tasks()] == [urgent["task_id"], routine["task_id"]]
    assert store.get_case("C1")["status"] == "escalated"
    store.resolve_nurse_task(routine["task_id"], "called back", hand_back=True)
    assert store.get_case("C1")["status"] == "awaiting_reply"
    assert [t["task_id"] for t in store.list_nurse_tasks()] == [urgent["task_id"]]


def test_routine_tasks_skip_the_weekend():
    store.set_now(datetime(2026, 9, 11, 16, 0))  # Friday afternoon
    t = store.create_nurse_task("C1", "clinical_question", "routine", "med question", "k3")
    assert t["due_at"].startswith("2026-09-14 17:00")  # Monday


def test_memory_takes_known_keys_only_and_the_latest_wins():
    with pytest.raises(StoreError):
        store.remember_preference("C1", "favourite_color", "blue", "x")
    store.remember_preference("C1", "part_of_day", "morning", "mornings are best")
    store.remember_preference("C1", "part_of_day", "afternoon", "actually I work mornings now")
    assert store.patient_summary("C1")["memory"] == {"part_of_day": "afternoon"}


def test_hand_back_returns_a_booked_patient_to_booked():
    store.book_appointment("C1", "S1", "kb")
    t = store.create_nurse_task("C1", "clinical_question", "routine", "med question", "k4")
    store.resolve_nurse_task(t["task_id"], "called back", hand_back=True)
    assert store.get_case("C1")["status"] == "booked"  # outreach must not offer visits again


def test_emergencies_are_due_now_and_top_the_queue():
    symptom = store.create_nurse_task("C1", "symptom", "urgent", "swelling", "k5")
    emergency = store.create_nurse_task("C2", "emergency", "urgent", "chest pain", "k6")
    assert emergency["due_at"].startswith("2026-09-09 09:00")  # now, not within the hour
    assert [t["task_id"] for t in store.list_nurse_tasks()][:2] == [emergency["task_id"], symptom["task_id"]]


def test_setup_wipes_every_outreach_table_including_new_ones():
    from readmit.outreach.setup import wipe_outreach

    store.create_nurse_task("C1", "callback", "routine", "wants a call", "k7")
    store.remember_preference("C1", "part_of_day", "afternoon", "I work mornings")
    tables = wipe_outreach()
    assert {"nurse_tasks", "patient_memory", "cases", "messages"} <= set(tables)
    with db.transaction() as c:
        for t in tables:
            assert db.one(c, f"select count(*) as n from outreach.{t}")["n"] == 0
