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


@pytest.mark.parametrize(
    "text",
    [
        "Yes the first one is fine. My legs have been a bit more puffy this week though.",
        "I gained 4 pounds since Monday and my breathing feels heavier at night.",
        "Sí, la primera. Tengo los pies hinchados.",
        "My shoes feel tight and I can't catch my breath on the stairs",
    ],
)
def test_a_symptom_wins_over_a_yes(text):
    assert read(text)["intent"] == "symptom"


def test_cost_is_a_barrier():
    for text in ("I can't afford another copay this month.", "No tengo dinero para pagar la visita"):
        assert read(text)["intent"] == "barrier"


def test_visit_type_and_time_of_day_are_read():
    r = read("A video visit would work. The first one is fine.")
    assert r["intent"] == "wants_visit" and r["accepts"] and r["details"] == {"mode": "telehealth"}
    r = read("I work mornings, so afternoons are always better for me.")
    assert r["details"] == {"part_of_day": "afternoon"} and r["standing"] is True
    r = read("Mornings are always best for me, I work afternoons.")
    assert r["details"] == {"part_of_day": "morning"} and r["standing"] is True
    assert read("Do you have anything in the afternoon instead?")["details"] == {"part_of_day": "afternoon"}
    assert read("Could we do a video visit instead of coming in?")["details"] == {"mode": "telehealth"}
