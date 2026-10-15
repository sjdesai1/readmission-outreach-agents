"""Simulated days: the sequencer runs the cadence end to end (stub agents, in-memory MCP servers)."""

import asyncio
from datetime import timedelta

import pytest

from readmit.agents.gateway import Gateway
from readmit.outreach import store
from readmit.sim.days import simulate

pytestmark = pytest.mark.usefixtures("pg")


def outbound(case_id):
    return [m for m in store.get_thread(case_id) if m["direction"] == "outbound"]


def test_a_week_with_no_replies_runs_the_full_cadence_then_closes():
    daily = asyncio.run(simulate(Gateway.in_memory(), days=7))
    assert len(daily) == 7
    for cid in ("C1", "C2"):
        assert len(outbound(cid)) == 3  # day 0, +48h, +96h
        case = store.get_case(cid)
        assert case["status"] == "closed" and "no reply after 3 attempts" in case["close_reason"]
    assert [m["channel"] for m in outbound("C2")] == ["sms", "sms", "phone"]  # last try is a call
    assert [m["channel"] for m in outbound("C1")] == ["email"] * 3  # no phone consent: stays on email


def test_attempts_are_spaced_and_only_in_daytime():
    asyncio.run(simulate(Gateway.in_memory(), days=5))
    times = [m["at"] for m in outbound("C1")]
    assert [t[:16] for t in times] == ["2026-09-09 09:00", "2026-09-11 09:00", "2026-09-13 09:00"]  # every 48h


def test_booked_and_escalated_cases_are_not_outreach_due():
    store.book_appointment("C1", "S1", "kb")
    store.create_nurse_task("C2", "callback", "routine", "wants a call", "kn")
    assert store.due_cases() == []
    store.set_now(store.now() + timedelta(days=3))
    assert store.due_cases() == []
