"""Language: requests, detection, the ask-once rule, interpreters, Spanish STOP words (ADR 0010).

Fixture patients: C1 Maria (English, email only), C2 Jose (Spanish, prefers sms).
"""

import asyncio

import pytest

from readmit.agents import language
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.agents.guards import screen_inbound
from readmit.nurse import console
from readmit.outreach import store


def turn(case_id, reply=None, channel="sms", agents=None):
    if reply:
        store.record_inbound(case_id, channel, reply)
    trigger = "inbound_reply" if reply else "outreach_due"
    return asyncio.run(run_turn(Gateway.in_memory(), case_id, trigger, reply, channel if reply else None, agents))


def outbound(case_id):
    return [m for m in store.get_thread(case_id) if m["direction"] == "outbound"]


# ------------------------------------------------------------------ pure rules
@pytest.mark.parametrize(
    "text, want",
    [
        ("ESPAÑOL", "Spanish"),
        ("Spanish please", "Spanish"),
        ("Can you text me in Spanish?", "Spanish"),
        ("¿Me puede escribir en español, por favor?", "Spanish"),
        ("en inglés por favor", "English"),
        ("Prefiero español", "Spanish"),
        ("Switch to English", "English"),
        ("I only speak Vietnamese", "Vietnamese"),
        ("Could you write to me in Vietnamese? My English is not good.", "Vietnamese"),
        ("The first one works for me.", None),
        ("I live in a small town", None),
    ],
)
def test_explicit_language_requests(text, want):
    assert language.requested(text) == want


@pytest.mark.parametrize(
    "text, want",
    [
        ("Sí, la primera opción está bien.", "Spanish"),
        ("The first one works for me.", "English"),
        ("Tôi không hiểu tin nhắn này", "Vietnamese"),
        ("我想预约第一个时间", "Chinese"),
        ("Obrigado, eu não posso ir na consulta", "Portuguese"),
        ("ok gracias", None),  # too short to tell
        ("2", None),
        ("STOP", None),
    ],
)
def test_detects_the_language_a_reply_is_written_in(text, want):
    assert language.detect(text) == want


def test_screen_decides_change_interpreter_or_ask_once():
    assert language.screen("Spanish please", "English", False) == {"action": "language_change", "language": "Spanish"}
    assert language.screen("Spanish please", "Spanish", False) == {"action": "continue"}  # already Spanish
    assert language.screen("Tôi không hiểu tin nhắn này", "Spanish", False)["action"] == "interpreter"
    assert language.screen("I only speak Vietnamese", "English", False) == {
        "action": "interpreter",
        "language": "Vietnamese",
    }
    assert language.screen("Sí, la primera opción está bien.", "English", False) == {
        "action": "continue",
        "ask": "Spanish",
    }
    assert language.screen("Sí, la primera opción está bien.", "English", True) == {"action": "continue"}  # once


@pytest.mark.parametrize("text", ["PARAR", "Basta!", "No más", "ALTO", "cancelar", "Stop."])
def test_spanish_stop_words_opt_out(text):
    assert screen_inbound(text)["action"] == "opt_out"


@pytest.mark.parametrize("text", ["para", "para el martes", "no más tarde de las 3"])
def test_ordinary_spanish_is_not_stop(text):
    assert screen_inbound(text)["action"] == "continue"


# ------------------------------------------------------------------ store
pg = pytest.mark.usefixtures("pg")


@pg
def test_stated_language_overrides_the_record_and_is_audited():
    r = store.set_language("C1", "Spanish", "¿me puede escribir en español?")
    assert r == {"language": "Spanish", "previous": "English"}
    patient = store.patient_summary("C1")["patient"]
    assert patient["preferred_language"] == "Spanish" and patient["language_source"] == "patient"
    assert store.patient_summary("C1")["memory"] == {}  # the language is not a scheduling preference
    events = [e["type"] for e in store.get_case("C1")["recent_events"]]
    assert "language_changed" in events


@pg
def test_unsupported_language_cannot_be_set():
    with pytest.raises(store.StoreError, match="interpreter"):
        store.set_language("C1", "Vietnamese", "I only speak Vietnamese")


@pg
def test_nurse_task_carries_the_interpreter_language():
    store.create_nurse_task("C2", "callback", "routine", "needs an interpreter", "kl1", interpreter="Vietnamese")
    [row] = console.queue()
    assert row["interpreter"] == "Vietnamese"


# ------------------------------------------------------------------ the graph
@pg
def test_asking_for_spanish_switches_and_offers_the_times_again_in_spanish():
    turn("C1")
    s = turn("C1", "¿Me puede escribir en español, por favor?", channel="email")
    assert "apply_language_change" in s["path"] and "scheduler" in s["path"]
    assert "reply_interpreter" not in s["path"]  # decided by the rule guard
    body = outbound("C1")[-1]["body"]
    assert "español" in body and "Horarios" in body
    assert store.patient_summary("C1")["patient"]["preferred_language"] == "Spanish"
    nxt = turn("C1", "Sí, la primera.", channel="email")
    assert nxt["schedule"]["appointment"]["status"] == "booked" and "reservada" in outbound("C1")[-1]["body"]


@pg
def test_a_booked_patient_who_switches_stays_booked():
    store.book_appointment("C1", "S2", "kl2")
    s = turn("C1", "Please write to me in Spanish from now on.", channel="email")
    assert "scheduler" not in s["path"]
    assert store.get_case("C1")["status"] == "booked"
    assert "español" in outbound("C1")[-1]["body"]


@pg
def test_writing_in_the_other_language_asks_once_and_never_switches_silently():
    turn("C1")
    turn("C1", "Sí, la primera opción está bien.", channel="email")
    assert outbound("C1")[-1]["body"].endswith(language.QUESTION["Spanish"])
    patient = store.patient_summary("C1")["patient"]
    assert patient["preferred_language"] == "English" and patient["language_asked"]
    turn("C1", "¿Dónde está la clínica? ¿Hay estacionamiento?", channel="email")
    assert language.QUESTION["Spanish"] not in outbound("C1")[-1]["body"]  # asked once


@pg
def test_a_reply_in_an_unsupported_language_goes_to_a_nurse_with_an_interpreter():
    turn("C2")
    s = turn("C2", "Tôi không hiểu tin nhắn này. Tôi chỉ nói tiếng Việt.")
    assert s["path"] == ["load_context", "input_guard", "escalate"]
    assert s["outcome"]["nurse_task"]["kind"] == "callback"
    [task] = store.list_nurse_tasks()
    assert task["interpreter"] == "Vietnamese"
    assert "intérprete" in outbound("C2")[-1]["body"]  # acknowledged in the language on file
    assert store.get_case("C2")["status"] == "escalated"


@pg
def test_asking_for_an_unsupported_language_goes_to_a_nurse():
    turn("C1")
    s = turn("C1", "Could you write to me in Vietnamese? My English is not good.", channel="email")
    assert s["outcome"]["action"] == "escalated"
    assert store.list_nurse_tasks()[0]["interpreter"] == "Vietnamese"
    assert store.patient_summary("C1")["patient"]["preferred_language"] == "English"


@pg
def test_emergency_beats_a_language_request():
    s = turn("C1", "No puedo respirar, escríbame en español", channel="email")
    assert s["guard"]["action"] == "emergency"


@pg
def test_paused_case_holds_a_language_request_for_the_nurse():
    store.create_nurse_task("C2", "callback", "routine", "asked for a call", "kl3")
    s = turn("C2", "Can you text me in English?")
    assert s["outcome"]["action"] == "held_for_nurse"
    assert store.patient_summary("C2")["patient"]["preferred_language"] == "Spanish"


@pg
def test_spanish_stop_closes_the_case():
    turn("C2")
    turn("C2", "BASTA")
    assert store.get_case("C2")["status"] == "opted_out"


@pg
def test_the_interpreter_is_the_second_net_for_language_requests():
    async def reads_language(state, gw):
        return {"intent": {"intent": "language_change", "accepts": False, "details": {"new_language": "spanish"}}}

    turn("C1")
    s = turn(
        "C1", "my reading is better in my first language", channel="email", agents={"reply_interpreter": reads_language}
    )
    assert "reply_interpreter" in s["path"] and "apply_language_change" in s["path"]
    assert store.patient_summary("C1")["patient"]["preferred_language"] == "Spanish"
