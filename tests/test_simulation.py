"""Simulated days: the sequencer runs the cadence end to end (stub agents, in-memory MCP servers)."""

import asyncio
from datetime import datetime, timedelta

import pytest

from readmit import db
from readmit.agents.gateway import Gateway
from readmit.outreach import store
from readmit.sim.days import outcomes_by_persona, simulate
from readmit.sim.outcomes import Outcome, report

pytestmark = pytest.mark.usefixtures("pg")


SILENT = {"C1": "silent", "C2": "silent"}


def run(days, **personas):
    return asyncio.run(simulate(Gateway.in_memory(), bad={}, days=days, personas=personas, cancel_rate=0.0))


def outbound(case_id):
    return [m for m in store.get_thread(case_id) if m["direction"] == "outbound"]


def test_a_week_with_no_replies_runs_the_full_cadence_then_closes():
    daily = asyncio.run(simulate(Gateway.in_memory(), bad={}, days=7, personas=SILENT))
    assert len(daily) == 7
    for cid in ("C1", "C2"):
        assert len(outbound(cid)) == 3  # day 0, +48h, +96h
        case = store.get_case(cid)
        assert case["status"] == "closed" and "no reply after 3 attempts" in case["close_reason"]
    assert [m["channel"] for m in outbound("C2")] == ["sms", "sms", "phone"]  # last try is a call
    assert [m["channel"] for m in outbound("C1")] == ["email"] * 3  # no phone consent: stays on email


def test_attempts_are_spaced_and_only_in_daytime():
    asyncio.run(simulate(Gateway.in_memory(), bad={}, days=5, personas=SILENT))
    times = [m["at"] for m in outbound("C1")]
    assert [t[:16] for t in times] == ["2026-09-09 09:00", "2026-09-11 09:00", "2026-09-13 09:00"]  # every 48h


def test_booked_and_escalated_cases_are_not_outreach_due():
    store.book_appointment("C1", "S1", "kb")
    store.create_nurse_task("C2", "callback", "routine", "wants a call", "kn")
    assert store.due_cases() == []
    store.set_now(store.now() + timedelta(days=3))
    assert store.due_cases() == []


def test_engaged_patient_books_and_opt_out_stops():
    run(1, C1="engaged", C2="opt_out")  # one day: booked, the visit hasn't happened yet
    assert store.get_case("C1")["status"] == "booked"
    assert store.get_case("C2")["status"] == "opted_out"
    assert outcomes_by_persona({"C1": "engaged", "C2": "opt_out"}) == {
        "engaged": {"booked": 1},
        "opt_out": {"opted_out": 1},
    }


def test_symptomatic_patient_goes_to_a_nurse_fast():
    run(2, C1="silent", C2="symptomatic")
    assert store.get_case("C2")["status"] == "escalated"
    [task] = store.list_nurse_tasks()
    assert task["kind"] == "symptom" and task["priority"] == "urgent"


def test_patients_never_answer_a_confirmation():
    run(1, C1="engaged", C2="silent")
    inbound = [m for m in store.get_thread("C1") if m["direction"] == "inbound"]
    assert len(inbound) == 1  # answered the offer, not the "you're booked" message


def test_no_show_patient_is_offered_new_times_and_rebooks():
    personas = {"C1": "engaged", "C2": "silent"}
    asyncio.run(
        simulate(Gateway.in_memory(), bad={}, days=3, personas=personas, no_show={"engaged": 1.0}, cancel_rate=0.0)
    )
    bodies = [m["body"] for m in outbound("C1")]
    assert any("missed you" in b for b in bodies)
    inbound = [m for m in store.get_thread("C1") if m["direction"] == "inbound"]
    assert len(inbound) >= 2  # picked a time, missed it, picked again


def test_clinic_cancellation_is_apologized_for():
    asyncio.run(
        simulate(Gateway.in_memory(), bad={}, days=2, personas={"C1": "engaged", "C2": "silent"}, cancel_rate=1.0)
    )
    assert any("clinic had to cancel" in m["body"] for m in outbound("C1"))


# ------------------------------------------------------------------ delivery failures and readmission
def test_bounced_email_with_no_other_channel_goes_to_a_nurse():
    personas = {"C1": "engaged", "C2": "silent"}
    asyncio.run(simulate(Gateway.in_memory(), days=1, personas=personas, bad={"C1": ("email",)}, cancel_rate=0.0))
    assert store.patient_summary("C1")["contact"]["undeliverable"] == ["email"]
    assert store.get_case("C1")["status"] == "escalated"  # nobody can reach her by message: a nurse calls


def test_wrong_number_switches_to_another_channel():
    personas = {"C1": "silent", "C2": "engaged"}
    asyncio.run(simulate(Gateway.in_memory(), days=1, personas=personas, bad={"C2": ("sms", "phone")}, cancel_rate=0.0))
    assert [m["channel"] for m in outbound("C2")][:2] == ["sms", "portal"]  # Jose: texts fail, the portal works


def test_readmission_stops_outreach_and_frees_the_visit():
    personas = {"C1": "engaged", "C2": "silent"}
    sure = {"E1": Outcome("E1", "C1", p0=1.0, rr=0.0, u=0.5, readmit_at=datetime(2026, 9, 9, 22, 0))}
    asyncio.run(simulate(Gateway.in_memory(), days=2, personas=personas, bad={}, outcomes=sure, cancel_rate=0.0))
    assert store.get_case("C1")["status"] == "readmitted"
    assert store.upcoming_appointment("C1") is None
    with db.transaction() as c:  # the slot went back to the clinic
        assert db.one(c, "select status from outreach.slots where slot_id = 'S1'")["status"] == "open"


# ------------------------------------------------------------------ outcomes with a real outreach effect
def test_an_attended_visit_prevents_the_readmission_it_would_have_had():
    personas = {"C1": "engaged", "C2": "silent"}
    later = datetime(2026, 9, 12, 12, 0)  # after the Thu Sep 10 visit
    draws = {
        "E1": Outcome("E1", "C1", p0=0.9, rr=0.5, u=0.6, readmit_at=later),  # 0.6 < 0.9 but not < 0.45
        "E2": Outcome("E2", "C2", p0=0.9, rr=0.5, u=0.6, readmit_at=later),  # never came in: readmitted
        "E9": Outcome("E9", None, p0=0.9, rr=0.5, u=0.6, readmit_at=later),  # holdout: usual care
    }
    run_with = {"no_show": {"engaged": 0.0}, "cancel_rate": 0.0, "bad": {}}
    asyncio.run(simulate(Gateway.in_memory(), days=4, personas=personas, outcomes=draws, **run_with))
    assert store.get_case("C1")["status"] == "completed"
    assert store.get_case("C2")["status"] == "readmitted"
    r = report()
    assert r["outreach"] == (0.5, 2) and r["holdout"] == (1.0, 1) and r["attended"] == (0.0, 1)
    assert abs(r["true_effect"] - (0.9 - 0.45) / 2) < 1e-9  # only C1 was treated
