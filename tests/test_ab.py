"""Prompt A/B statistics and text metrics (pure)."""

from readmit.evals.ab import compare, fk_grade, mcnemar, paired, word_count


def test_reading_grade_is_lower_for_plain_words():
    plain = "We can see you Thursday. Reply 1 to book it."
    dense = (
        "Subsequent to your hospitalization, a comprehensive evaluation is recommended at your earliest convenience."
    )
    assert fk_grade(plain) < fk_grade(dense)
    assert fk_grade("Hola Jose, su visita es el jueves.") is None  # Spanish: not scored with an English formula


def test_mcnemar_only_counts_disagreements():
    a = [True] * 30 + [False] * 5
    b = [True] * 30 + [True] * 5
    m = mcnemar(a, b)
    assert m["b_only"] == 5 and m["a_only"] == 0 and m["p"] < 0.1
    assert mcnemar([True, False], [True, False])["p"] == 1.0


def test_paired_difference_and_interval():
    a = [40, 50, 60, 45, 55, 52, None]
    b = [30, 41, 49, 36, 44, 40, 10]
    m = paired(a, b)
    assert m["n"] == 6 and m["diff"] < 0 and m["ci95"][1] < 0
    assert paired([1, 2], [1, 2]) is None  # too few to say anything


def test_compare_shapes():
    row = {"passed": True, "words": 40, "grade": 6.0, "rewritten": 0.0, "advisory": 0.0}
    r = compare([row] * 6, [{**row, "words": 30}] * 6)
    assert r["words"]["diff"] == -10 and r["primary"]["p"] == 1.0
    assert word_count("Hi there, Maria") == 3
