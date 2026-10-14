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
