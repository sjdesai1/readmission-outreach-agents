"""Checking the judge: planted errors and agreement with a person. Pure functions, no LLM."""

from readmit.evals import judge_check as C
from readmit.evals.judge import CRITERIA

EN = {
    "id": "turns:book",
    "language": "English",
    "channel": "email",
    "conversation": [{"from": "patient", "text": "The first one works."}],
    "message": "Hi Maria, your visit is booked for Thu Sep 10, 9:00 AM. Reply here if you need to change it.",
    "facts": {"appointment": {"start": "2026-09-10T09:00"}},
}
ES = {
    **EN,
    "id": "turns:libro",
    "language": "Spanish",
    "message": "Hola Jose, su visita es el jue 10 sep, 9:00 AM. Responda aquí si necesita cambiarla.",
}


def test_each_flaw_changes_the_message_and_names_its_criterion():
    for flaw in C.FLAWS:
        p = C.plant(EN, flaw)
        assert p and p["message"] != EN["message"] and p["target"] in CRITERIA, flaw
    assert "4:45 PM" in C.plant(EN, "wrong_time")["message"]
    assert "ibuprofeno" in C.plant(ES, "medical_advice")["message"]
    assert "tu visita" in C.plant(ES, "bad_language")["message"]
    assert "Reply" not in C.plant(EN, "no_next_step")["message"]


def test_flaws_that_cannot_apply_are_skipped():
    no_facts = {**EN, "facts": None}
    assert C.plant(no_facts, "wrong_time") is None  # nothing to contradict
    first_message = {**EN, "conversation": []}
    assert C.plant(first_message, "ignores_patient") is None  # nothing to ignore
    assert C.plant({**EN, "message": "Reply YES or NO?"}, "no_next_step") is None  # nothing left


def test_planted_set_takes_up_to_n_per_flaw():
    items = [{**EN, "id": f"i{i}"} for i in range(5)]
    s = C.planted_set(items, per_flaw=2)
    assert len(s) == 2 * len(C.FLAWS) and len({x["id"] for x in s}) == len(s)


def judged(verdicts, **kw):
    return {"id": "x", "verdicts": dict.fromkeys(CRITERIA, "yes") | verdicts, **kw}


def test_planted_score_counts_the_target_criterion_and_false_alarms():
    planted = [
        judged({"no_medical_advice": "no"}, flaw="medical_advice", target="no_medical_advice"),
        judged({"plain_language": "no"}, flaw="medical_advice", target="no_medical_advice"),  # flagged the wrong thing
        judged({}, flaw="jargon", target="plain_language"),
    ]
    s = C.score_planted(planted, [judged({}), judged({"warm_respectful_tone": "no"}, id="o2")])
    assert s["caught"] == {"n": 1, "total": 3, "rate": 1 / 3}
    assert s["by_flaw"] == {"medical_advice": {"n": 1, "total": 2}, "jargon": {"n": 0, "total": 1}}
    assert s["false_alarms"]["n"] == 1 and s["false_alarms"]["ids"] == ["o2"]


def test_kappa_corrects_for_chance():
    assert C.kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert C.kappa([True, True, False, False], [True, False, True, False]) == 0.0
    # both pass 90%: 82% agreement by chance alone, so 80% agreement is below chance
    a = [True] * 9 + [False]
    b = [True] * 8 + [False, True]
    assert round(C.kappa(a, b), 2) == -0.11


def test_agreement_shows_where_the_judge_is_lenient_or_strict():
    yes = dict.fromkeys(CRITERIA, "yes")
    labels = [
        {"id": "a", "human_pass": True, "human_failed": [], "judge_verdicts": yes},
        {"id": "b", "human_pass": False, "human_failed": ["plain_language"], "judge_verdicts": yes},
        {"id": "c", "human_pass": True, "human_failed": [], "judge_verdicts": yes | {"clear_next_step": "no"}},
        {"id": "d", "human_pass": False, "human_failed": ["clear_next_step"],
         "judge_verdicts": yes | {"clear_next_step": "no"}},
    ]  # fmt: skip
    a = C.agreement(labels)
    assert a["agree"] == 2 and a["lenient"] == ["b"] and a["strict"] == ["c"]
    assert a["by_criterion"]["clear_next_step"] == {"both": 1, "only_you": 0, "only_judge": 1}
    assert a["by_criterion"]["plain_language"] == {"both": 0, "only_you": 1, "only_judge": 0}


def test_label_sample_is_seeded_and_mixes_judge_passes_and_failures():
    items = [judged({}, id=f"p{i}") for i in range(30)] + [
        judged({"plain_language": "no"}, id=f"f{i}") for i in range(10)
    ]
    s = C.label_sample(items, n=20)
    assert len(s) == 20 and s == C.label_sample(items, n=20)
    assert sum(x["id"].startswith("f") for x in s) == 8


def test_a_single_swapped_word_is_not_a_planted_language_error():
    one = {**ES, "message": "Hola Jose, podemos ayudar con el transporte. Su visita es el jueves."}  # only "Su"
    assert C.plant(one, "bad_language") is None
    formal = {**ES, "message": "Una enfermera se comunicará con usted. Si algo empeora, llame a su médico."}
    assert (
        C.plant(formal, "bad_language")["message"] == "Una enfermera te contactará. Si algo empeora, llama a tu médico."
    )
