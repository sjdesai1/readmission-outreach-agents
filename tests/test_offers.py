"""'The first one' means the slot the patient actually saw: offers are stored when sent and looked up on reply."""

import asyncio

import pytest

from readmit import db
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.agents.state import picked_option
from readmit.outreach import store


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("The first one works.", 1),
        ("Sí, la primera.", 1),
        ("1968, and yes the first one works", 1),
        ("the 2nd please", 2),
        ("option 3", 3),
        ("2", 2),
        ("la última", -1),
        ("see you at 2 PM", None),
        ("yes", None),
        ("a las dos", None),
    ],
)
def test_which_option_a_reply_picks(text, n):
    assert picked_option(text) == n


def turn(case_id, reply=None, channel="email"):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None))


@pytest.mark.usefixtures("pg")
def test_offers_are_stored_in_the_order_the_patient_saw_them():
    s = turn("C1")
    offers = store.offered_slots("C1")
    assert [o["slot_id"] for o in offers] == [x["slot_id"] for x in s["schedule"]["offered"]]
    assert all(o["still_open"] for o in offers) and [o["option"] for o in offers] == [1, 2, 3][: len(offers)]


@pytest.mark.usefixtures("pg")
def test_the_second_one_books_the_second_slot_they_saw():
    turn("C1")
    second = store.offered_slots("C1")[1]
    turn("C1", "The second one works for me.")
    with db.transaction() as c:
        booked = db.one(c, "select slot_id from outreach.appointments where case_id = 'C1' and status = 'booked'")
    assert booked["slot_id"] == second["slot_id"]


@pytest.mark.usefixtures("pg")
def test_a_picked_time_that_is_gone_is_never_swapped_for_another():
    turn("C1")
    first = store.offered_slots("C1")[0]
    with db.transaction() as c:
        db.rows(c, "update outreach.slots set status = 'held_other' where slot_id = :s", s=first["slot_id"])
    s = turn("C1", "The first one works.")
    assert s["schedule"]["appointment"] is None and "no longer open" in s["schedule"]["note"]
    assert "no longer open" in store.get_thread("C1")[-1]["body"]
    assert first["slot_id"] not in [o["slot_id"] for o in store.offered_slots("C1")]  # the new offer replaced it
