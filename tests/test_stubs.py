"""The rule-based agents: they run in tests, in free simulations, and whenever the LLM budget is spent."""

import asyncio

import pytest

from readmit.agents import stubs


def read(text):
    return asyncio.run(stubs.reply_interpreter({"inbound": text}, None))["intent"]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("I missed yesterday's water pill. Should I take two today?", "clinical_question"),
        ("Is it normal to feel this tired?", "clinical_question"),
        ("If you text me again I'll make you regret it", "abusive"),
        ("The first one works.", "wants_visit"),
        ("The second time is better for me", "wants_visit"),
    ],
)
def test_rule_based_reading(text, intent):
    assert read(text)["intent"] == intent


def test_a_yes_with_its_own_time_is_not_a_yes_to_our_options():
    assert read("Book me for yesterday at 3 AM")["accepts"] is False
    assert read("Just book me Saturday at 6 PM with Dr. House")["accepts"] is False
    assert read("Yes, the first one works")["accepts"] is True
    assert read("yes please")["accepts"] is True
