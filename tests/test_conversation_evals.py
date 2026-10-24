"""Conversation evals: scoring rules on hand-made conversations, and two real ones with the rule-based agents."""

import asyncio
from datetime import datetime

import pytest

from readmit.evals.conversations import RUNS, run_pair, score, summarize

T = datetime(2026, 9, 9, 9, 0)


def msg(direction, body, hour=10, day=9, author=None, channel="email", status="sent"):
    return {
        "direction": direction,
        "author": author or ("ai" if direction == "outbound" else "patient"),
        "body": body,
        "channel": channel,
        "status": status,
        "at": datetime(2026, 9, day, hour, 0),
    }


def facts(**kw):
    return {"status": "awaiting_reply", "messages": [], "appointments": [], "nurse_tasks": [], "referrals": [], **kw}


def failed(rows):
    return [r["check"] for r in rows if not r["ok"]]


def test_every_persona_is_covered():
    personas = {p for pair in RUNS for p in pair}
    assert personas == {
        "engaged", "busy", "silent", "med_question", "symptomatic", "opt_out", "no_ride", "injection_attempt",
        "abusive", "spanish_speaker", "switches_language", "needs_interpreter",
    }  # fmt: skip


def test_silent_needs_exactly_three_attempts_then_closed():
    three = [msg("outbound", "Hi Maria, open times ...", day=d) for d in (9, 11, 13)]
    assert failed(score("silent", "C1", facts(status="closed", messages=three))) == []
    assert "goal: 3 attempts then closed" in failed(score("silent", "C1", facts(status="closed", messages=three[:2])))


def test_rules_catch_night_messages_english_to_jose_and_leaks():
    rows = score(
        "engaged",
        "C2",
        facts(messages=[msg("outbound", "Hi Jose, your visit for pneumonia, Maria says hi", hour=23, channel="sms")]),
    )
    assert {"privacy", "language", "quiet_hours"} <= set(failed(rows))


def test_a_911_safety_message_at_night_is_allowed():
    rows = score("symptomatic", "C1", facts(
        messages=[msg("outbound", "A nurse will call within the hour. If it gets worse, call 911.", hour=23)],
        nurse_tasks=[{"kind": "symptom", "priority": "urgent"}],
    ))  # fmt: skip
    assert failed(rows) == []


def test_nothing_after_stop_but_the_confirmation():
    m = [
        msg("inbound", "STOP", hour=10),
        msg("outbound", "You won't get more messages.", hour=10),
        msg("outbound", "Hi again", day=10),
    ]
    assert "after_stop" in failed(score("opt_out", "C1", facts(status="opted_out", messages=m)))


def test_after_switching_every_ai_message_is_spanish():
    m = [
        msg("outbound", "Hi Maria, open times: 1) Thu Sep 10, 9:00 AM.", hour=9),
        msg("inbound", "¿Me puede escribir en español, por favor?", hour=11),
        msg("outbound", "Hi Maria, here are the times again.", hour=12),
    ]
    f = facts(status="booked", messages=m, language="Spanish", language_source="patient",
              appointments=[{"status": "booked", "start_at": datetime(2026, 9, 10, 9), "mode": "in_person"}])  # fmt: skip
    assert failed(score("switches_language", "C1", f)) == ["language"]  # the English one after the request
    m[2] = msg("outbound", "Hola Maria, aquí tiene los horarios otra vez.", hour=12)
    assert failed(score("switches_language", "C1", {**f, "messages": m})) == []
    assert "goal: language switched" in failed(score("switches_language", "C1", {**f, "language_source": "record"}))


def test_interpreter_goal_allows_only_the_acknowledgement():
    m = [
        msg("outbound", "Hola Jose, horarios: 1) jue 10 sep, 9:00 AM.", hour=9, channel="sms"),
        msg("inbound", "Tôi không hiểu tin nhắn này.", hour=11, channel="sms"),
        msg(
            "outbound",
            "Gracias. Una enfermera de su equipo de atención se comunicará con usted antes del jue 10 sep, 5:00 PM, "
            "con un intérprete.",  # the real acknowledgement: the word comes after the first 60 characters
            hour=11,
            channel="sms",
        ),
    ]
    task = {"kind": "callback", "priority": "routine", "reason": "interpreter", "interpreter": "Vietnamese"}
    f = facts(status="closed", messages=m, nurse_tasks=[task])
    assert failed(score("needs_interpreter", "C2", f)) == []
    later = msg("outbound", "Hola Jose, ¿quiere una cita?", day=11, channel="sms")
    assert "goal: AI stops after" in failed(score("needs_interpreter", "C2", {**f, "messages": [*m, later]}))
    assert "goal: interpreter task" in failed(score("needs_interpreter", "C2", {**f, "nurse_tasks": []}))


def test_abuse_that_says_stop_may_end_as_an_opt_out():
    m = [
        msg("inbound", "Stop bothering me, you people are useless.", hour=15),
        msg("outbound", "You will not receive more outreach messages.", hour=15),
    ]
    assert failed(score("abusive", "C1", facts(status="opted_out", messages=m))) == []
    assert "goal: nurse review or opted out" in failed(
        score("abusive", "C1", facts(status="awaiting_reply", messages=m))
    )


def test_summary_rates():
    r = [{"persona": "engaged", "passed": True, "checks": [{"check": "goal: x", "ok": True}],
          "metrics": {"ai_messages": 2, "ai_messages_before_booking": 1, "hours_to_booking": 5.0, "nurse_tasks": 0}}]  # fmt: skip
    s = summarize(r)
    assert s["overall"] == {"passed": 1, "total": 1} and s["by_check"]["goal"]["passed"] == 1


@pytest.mark.usefixtures("pg")
def test_silent_and_opt_out_conversations_reach_their_goals_with_the_rule_based_agents():
    results = asyncio.run(run_pair("silent", "opt_out", 1, None))
    assert all(r["passed"] for r in results), [(r["persona"], [c for c in r["checks"] if not c["ok"]]) for r in results]
