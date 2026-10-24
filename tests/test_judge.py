"""LLM judge: which messages become items, and how verdicts are counted. No LLM calls here."""

from readmit.evals.judge import (
    CRITERIA,
    Judgment,
    dedupe,
    enforce_na,
    failures,
    items_from_conversations,
    items_from_turns,
    summarize,
)

CASES = [
    {"id": "book", "category": "booking", "case": "C2", "reply": "Sí, la primera.", "channel": "sms"},
    {"id": "esc", "category": "symptom", "case": "C1", "reply": "My ankles are swollen", "channel": "email"},
    {"id": "out", "category": "outreach", "case": "C1", "trigger": "outreach_due"},
]


def turn_report():
    given = {"offered": [], "appointment": {"start": "2026-09-10T09:00", "visit": "clinic visit", "doctor": "Dr. Test"}}
    return {
        "results": [
            {"id": "book", "category": "booking", "facts": {"action": "sent", "drafted": "Hola {first_name}...",
             "sent": ["Hola Jose, su visita está reservada."], "language": "Spanish", "given": given,
             "before": [{"from": "ai", "channel": "sms", "text": "Horarios: 1) jue 10 sep, 9:00 AM."}]}},
            {"id": "esc", "category": "symptom", "facts": {"action": "escalated", "drafted": None,
             "sent": ["A nurse will call you within the hour."], "language": "English"}},
            {"id": "out", "category": "outreach", "facts": {"action": "sent", "drafted": "Hi...",
             "sent": ["Hi Maria, open times: 1) Thu Sep 10, 9:00 AM."], "language": "English", "given": None}},
        ]
    }  # fmt: skip


def test_turn_items_are_the_writer_messages_that_were_sent():
    items = items_from_turns(turn_report(), CASES, "turns")
    assert [i["id"] for i in items] == ["turns:book", "turns:out"]  # the escalation's fixed template is skipped
    book = items[0]
    assert book["language"] == "Spanish" and book["message"] == "Hola Jose, su visita está reservada."
    assert book["conversation"] == [  # the offer the reply refers to, then the reply
        {"from": "ai", "channel": "sms", "text": "Horarios: 1) jue 10 sep, 9:00 AM."},
        {"from": "patient", "channel": "sms", "text": "Sí, la primera."},
    ]
    assert book["facts"]["appointment"]["doctor"] == "Dr. Test"
    assert items[1]["conversation"] == [] and items[1]["facts"] is None


def msg(author, body):
    return {"at": "2026-09-09 09:00", "author": author, "channel": "email", "body": body}


def test_conversation_items_carry_the_messages_before_them():
    report = {"results": [{"persona": "switches_language", "case": "C1", "seed": 1, "transcript": [
        msg("ai", "Hi Maria, open times: 1) ..."),
        msg("patient", "¿Me puede escribir en español?"),
        msg("ai", "Gracias Maria, desde ahora le escribiremos en español."),
        msg("nurse", "This is Nurse Kim."),
    ]}]}  # fmt: skip
    items = items_from_conversations(report)
    assert len(items) == 2  # the nurse's message is not graded
    assert items[0]["language"] == "English" and items[0]["conversation"] == []
    assert items[1]["language"] == "Spanish" and [c["from"] for c in items[1]["conversation"]] == ["ai", "patient"]


def test_the_same_message_in_the_same_context_is_graded_once():
    a = {"language": "English", "message": "Reminder: your visit is Fri.", "conversation": []}
    assert len(dedupe([a, dict(a), {**a, "language": "Spanish"}])) == 2


def judged(verdicts, message="Hi Maria, your visit is booked.", source="turns"):
    return {"id": "x", "source": source, "message": message, "verdicts": verdicts,
            "reasons": dict.fromkeys(CRITERIA, "because")}  # fmt: skip


def test_rates_leave_out_na_and_ungraded_messages():
    ok = dict.fromkeys(CRITERIA, "yes") | {"consistent_with_facts": "n/a"}
    bad = ok | {"plain_language": "no"}
    s = summarize([judged(ok), judged(bad, message="word " * 40), {"id": "y", "source": "turns", "verdicts": None}])
    assert s["messages"] == 3 and s["graded"] == 2
    assert s["all_criteria"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert s["by_criterion"]["plain_language"]["rate"] == 0.5
    assert s["by_criterion"]["consistent_with_facts"]["graded"] == 0  # n/a everywhere: no rate
    assert s["words"]["failing_mean"] == 40 and s["words"]["passing_mean"] == 6


def test_failures_list_each_failed_criterion_with_the_reason():
    v = dict.fromkeys(CRITERIA, "yes") | {"warm_respectful_tone": "no", "language_quality": "no"}
    assert [f["criterion"] for f in failures([judged(v)])] == ["warm_respectful_tone", "language_quality"]


def test_the_judgment_schema_requires_a_verdict_for_every_criterion():
    full = {c: {"reason": "r", "verdict": "yes"} for c in CRITERIA}
    assert Judgment.model_validate(full)
    try:
        Judgment.model_validate({c: v for c, v in full.items() if c != "plain_language"})
    except ValueError:
        return
    raise AssertionError("a missing criterion must fail validation")


def test_code_decides_when_two_criteria_do_not_apply():
    yes = dict.fromkeys(CRITERIA, "yes")
    reply = {"facts": None, "conversation": [{"from": "ai", "text": "..."}, {"from": "patient", "text": "2"}]}
    assert enforce_na(reply, yes)["consistent_with_facts"] == "n/a"  # no facts given
    assert enforce_na(reply, yes)["answers_the_patient"] == "yes"
    after_nurse = {"facts": {"offered": []}, "conversation": [{"from": "patient"}, {"from": "nurse"}]}
    out = enforce_na(after_nurse, yes | {"answers_the_patient": "no"})
    assert out["answers_the_patient"] == "n/a" and out["consistent_with_facts"] == "yes"
