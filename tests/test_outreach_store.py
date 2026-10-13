"""Integration tests: the outreach rules against a real Postgres (a separate `app_test` database).

Skipped automatically when Postgres isn't reachable. CI runs them with a Postgres service container.
"""
from datetime import datetime

import pytest

from readmit.outreach import store
from readmit.outreach.store import StoreError

pytestmark = pytest.mark.usefixtures("pg")


def test_summary_is_minimum_necessary():
    s = store.patient_summary("C1")
    assert s["patient"]["first_name"] == "Maria"
    assert "maria@example.com" not in str(s)          # agents never see contact details
    assert s["contact"]["allowed_channels"] == ["email"]
    assert s["risk"]["barriers"] == ["transport_barrier"]


def test_find_slots_skips_taken_too_soon_and_disallowed_telehealth():
    ids = [s["slot_id"] for s in store.find_slots("C2", limit=10)]   # P2 can't do telehealth
    assert ids == ["S1", "S2"]        # S3 telehealth, S4 under 2h away, S5 taken by someone else


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
    for action in (lambda: store.book_appointment("C1", "S1", "early-1"),
                   lambda: store.send_message("C1", "email", "hi", "early-2"),
                   lambda: store.create_referral("C1", "transport", "no car", "early-3"),
                   lambda: store.update_case("C1", "contacted", "early")):
        with pytest.raises(StoreError, match="not open yet"):
            action()
    assert store.get_case("C1")["recent_events"] == []    # reads still work, nothing was written
