"""Extraction safeguards that don't need an LLM: grounding and scoring."""

import pandas as pd

from readmit.extract.evaluate import score
from readmit.extract.social import grounded
from readmit.sim.notes import SOCIAL_FACTORS

NOTE = "SH: Lives alone in apt.  No car, relies on bus.\nDenies ETOH/drugs."


def test_exact_quote_is_grounded():
    assert grounded("Lives alone in apt.", NOTE)


def test_whitespace_and_case_differences_still_grounded():
    assert grounded("no car,   relies on BUS.", NOTE)


def test_paraphrase_is_rejected():
    assert not grounded("patient lives by themselves", NOTE)


def test_empty_evidence_is_rejected():
    assert not grounded(None, NOTE) and not grounded("", NOTE)


def test_scoring_counts_tp_fp_fn():
    pred = pd.DataFrame(
        [
            {
                "encounter_id": "A",
                "extracted": True,
                **{f: False for f in SOCIAL_FACTORS},
                "lives_alone": True,
                "substance_use": True,
            }
        ]
    )
    truth = pd.DataFrame(
        [{"encounter_id": "A", **{f: False for f in SOCIAL_FACTORS}, "lives_alone": True, "transport_barrier": True}]
    )
    r = score(pred, truth)["per_factor"]
    assert (r["lives_alone"]["tp"], r["substance_use"]["fp"], r["transport_barrier"]["fn"]) == (1, 1, 1)


# ------------------------------------------------------------------ keyword baseline (make demo without a key)
def test_keyword_rules_find_every_way_the_notes_write_a_factor():
    from readmit.extract.social import extract_rules
    from readmit.sim.notes import SOCIAL_POS

    for factor, sentences in SOCIAL_POS.items():
        for s in sentences:
            assert factor in extract_rules(s), s


def test_keyword_rules_skip_denials_relatives_and_family_history():
    from readmit.extract.social import extract_rules
    from readmit.sim.notes import FHX_FATHER, FHX_MOTHER, SOCIAL_NEG, TRAPS

    family = f"FHx: {', '.join(FHX_MOTHER + FHX_FATHER)}."
    for s in [*(x for xs in SOCIAL_NEG.values() for x in xs), *TRAPS, family]:
        assert extract_rules(s) == {}, s


def test_keyword_rows_are_grounded_and_one_per_stay():
    from readmit.extract.social import rules_rows

    texts = {"E1": "[progress | 2026-09-01 08:00]\nLives alone in apt. Denies ETOH/drugs.", "E2": "Has own car."}
    rows = rules_rows(texts, ["E1", "E2", "E3"])
    assert [r["encounter_id"] for r in rows] == ["E1", "E2", "E3"]
    assert rows[0]["lives_alone"] and not rows[0]["substance_use"] and grounded("Lives alone in apt.", texts["E1"])
    assert not any(rows[1][f] for f in SOCIAL_FACTORS) and not any(rows[2][f] for f in SOCIAL_FACTORS)
