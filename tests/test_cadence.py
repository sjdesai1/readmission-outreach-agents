"""Outreach cadence rules: pure functions, no database."""

from datetime import datetime

from readmit.outreach.cadence import in_outreach_hours, next_attempt, unanswered


def msgs(*directions):
    return [{"direction": d} for d in directions]


def test_unanswered_counts_only_since_the_patient_last_wrote():
    assert unanswered([]) == 0
    assert unanswered(msgs("outbound", "outbound")) == 2
    assert unanswered(msgs("outbound", "inbound", "outbound")) == 1
    assert unanswered(msgs("outbound", "inbound")) == 0


def test_third_attempt_is_a_call_only_if_calls_are_allowed():
    assert next_attempt(0, ["sms", "phone"]) == {"close": False, "attempt": 1, "channel": None}
    assert next_attempt(2, ["sms", "phone"])["channel"] == "phone"
    assert next_attempt(2, ["email"])["channel"] is None


def test_close_after_three_unanswered_attempts():
    assert next_attempt(3, ["sms"])["close"] is True


def test_outreach_only_in_daytime_hours():
    assert in_outreach_hours(datetime(2026, 9, 9, 9, 0))
    assert not in_outreach_hours(datetime(2026, 9, 9, 19, 0))
    assert not in_outreach_hours(datetime(2026, 9, 9, 7, 0))
