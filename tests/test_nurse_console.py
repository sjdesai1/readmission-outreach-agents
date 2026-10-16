"""Nurse queue screen logic, against the real store (the Streamlit file only draws it)."""

import pytest

from readmit.nurse import console
from readmit.outreach import store

pytestmark = pytest.mark.usefixtures("pg")


def test_queue_shows_who_the_patient_is_and_what_is_late():
    store.create_nurse_task("C2", "symptom", "urgent", "swelling", "kq1")
    [row] = console.queue()
    assert row["patient"] == "Jose" and row["language"] == "Spanish" and row["minutes_late"] == 0


def test_nurse_writes_in_the_same_thread_as_the_ai():
    t = store.create_nurse_task("C2", "callback", "routine", "wants a call", "kq2")
    store.send_message("C2", "sms", "Hi Jose, open times: 1) Thu Sep 10, 9:00 AM.", "kq3")
    console.reply(t["task_id"], "C2", "sms", "Hola Jose, soy la enfermera Kim.")
    console.reply(t["task_id"], "C2", "sms", "Hola Jose, soy la enfermera Kim.")  # double click: sent once
    assert [m["author"] for m in console.case_view("C2")["thread"]] == ["ai", "nurse"]
    console.resolve(t["task_id"], "called back", hand_back=True)
    assert store.get_case("C2")["status"] == "awaiting_reply" and console.queue() == []


def test_capacity_counts_open_work():
    store.create_nurse_task("C1", "callback", "routine", "call me", "kq4")
    store.create_nurse_task("C2", "symptom", "urgent", "swelling", "kq5")
    cap = console.capacity()
    assert cap["urgent_open"] == 1 and sum(cap["routine_by_day"].values()) == 1
