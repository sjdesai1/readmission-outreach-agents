"""Case timeline (milestone 7): one case's story, in order, from the tables the system already writes.

Fixture patients: C1 Maria (English, email, discharged Sep 7 noon, high risk 41%), C2 Jose (Spanish, sms).
"""

import asyncio

import pytest

from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.evals.fixture import NOW
from readmit.outreach import store
from readmit.viewer import timeline


def turn(case_id, reply=None, channel="sms"):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None))


def titles(t):
    return [i["title"] for i in t["items"]]


def first(t, prefix):
    return next(n for n, title in enumerate(titles(t)) if title.startswith(prefix))


# ------------------------------------------------------------------ pure wording
def test_an_intent_note_reads_as_a_decision():
    assert timeline.describe("note", {"note": "intent: wants_visit"}) == (
        "decision",
        "ai",
        "Read the reply as: wants visit",
        "",
    )


@pytest.mark.parametrize("type_", ["message_sent", "message_received", "message_held", "nurse_task_created"])
def test_events_shown_from_their_own_tables_are_not_doubled(type_):
    assert timeline.describe(type_, {}) is None


def test_an_escalation_note_is_left_to_the_nurse_task():
    assert timeline.describe("note", {"note": "escalated (symptom, urgent): legs swelling"}) is None


def test_a_rescore_says_which_way_the_risk_moved():
    kind, _, title, detail = timeline.describe(
        "rescored", {"trigger": "ed_visit", "day": 3.5, "before": 0.3, "after": 0.45}
    )
    assert kind == "risk" and title == "Re-scored 30% ↑ 45%" and detail == "ed visit, day 3.5"
    assert "→" in timeline.describe("rescored", {"trigger": "landmark", "day": 7, "before": 0.3, "after": 0.31})[2]


def test_an_unknown_event_still_shows():
    assert timeline.describe("something_new", {})[2] == "something new"


# ------------------------------------------------------------------ built from the store
pg = pytest.mark.usefixtures("pg")


@pg
def test_a_booking_reads_top_to_bottom():
    turn("C1")
    turn("C1", "Yes, the first one works for me.", "email")
    t = timeline.build("C1")
    assert titles(t)[0] == "Discharged: Heart failure"
    assert titles(t)[1].startswith("Flagged high risk (41%)")
    # the fixture's clock doesn't move between turns, so this is the audit log's own order, not the clock's
    wrote, read, booked = first(t, "Patient wrote"), first(t, "Read the reply as"), first(t, "Booked a")
    last_sent = max(n for n, title in enumerate(titles(t)) if title == "AI sent")
    assert first(t, "AI sent") < wrote < booked < last_sent < read  # the intent is noted after the reply is sent
    reply = t["items"][wrote]
    assert reply["body"].startswith("Yes, the first") and reply["channel"] == "email"
    assert reply["day"] == pytest.approx(1.875, abs=0.01)  # Sep 9, 9 AM is 1 day 21 hours after the discharge
    assert t["header"]["status"] == "booked" and t["counts"]["message"] >= 3


@pg
def test_a_rescore_and_a_readmission_end_the_story():
    store.rescore("C1", 0.6, 7)
    store.mark_readmitted("C1", NOW)
    t = timeline.build("C1")
    assert t["risk"] == [(0.0, 0.41), (7.0, 0.6)]
    assert any(x.startswith("Re-scored 40% ↑ 60%") for x in titles(t))
    assert titles(t)[-1] == "Readmitted to the hospital"
    assert not any(x.startswith("Case closed") for x in titles(t))  # said once, as the readmission


@pg
def test_an_interpreter_handoff_is_shown_and_is_a_shortcut():
    turn("C2")
    turn("C2", "Tôi không hiểu tin nhắn này. Tôi chỉ nói tiếng Việt.")
    t = timeline.build("C2")
    [handoff] = [i for i in t["items"] if i["kind"] == "nurse"]
    assert "interpreter: Vietnamese" in handoff["title"]
    shortcuts = {x["label"]: x["case_id"] for x in timeline.interesting()}
    assert shortcuts["Needed an interpreter"] == "C2"
    assert "Readmitted" not in shortcuts  # only stories that happened


@pg
def test_the_picker_lists_the_busiest_case_first():
    turn("C1")
    rows = timeline.cases()
    assert [r["case_id"] for r in rows] == ["C1", "C2"] and rows[0]["first_name"] == "Maria"
