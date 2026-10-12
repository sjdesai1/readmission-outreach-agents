"""Model plumbing that must never silently break: feature alignment, metrics, tiers, holdout."""
import numpy as np
import pandas as pd

from readmit.model import features
from readmit.model.metrics import paired_auroc_diff, precision_at, recall_at
from readmit.model.score import in_holdout, tier, top_drivers


def stays(n=4, **overrides):
    base = {"encounter_id": [f"E{i}" for i in range(n)], "age": 70, "adi_decile": 5, "length_of_stay_days": 4,
            "charlson_index": 3, "prior_admissions_12m": 1, "ed_visits_6m": 0, "prior_30d_readmits_12m": 0,
            "hemoglobin": 12.0, "sodium": 138.0, "creatinine": 1.0, "albumin": np.nan, "discharge_sbp": 120,
            "num_discharge_meds": 8, "med_changes_count": 1, "sex": "F", "payer": "Medicare",
            "admission_type": "Emergency", "primary_dx_group": "Pneumonia", "discharge_disposition": "Home"}
    base |= {c: False for c in features.BOOLEAN}
    base |= overrides
    return pd.DataFrame(base)


def test_columns_identical_even_when_categories_missing():
    """A live batch with no Sepsis patients must still produce the same columns as training."""
    a = features.build(stays(primary_dx_group="Sepsis"))
    b = features.build(stays(primary_dx_group="Pneumonia"))
    assert list(a.columns) == list(b.columns)


def test_missing_albumin_becomes_an_indicator():
    X = features.build(stays())
    assert (X["albumin_missing"] == 1).all()


def test_lace_score_example():
    # LOS 4 -> 4, emergency -> 3, charlson 3 -> 3, 0 ED visits -> 0
    assert features.lace(stays(n=1)).iloc[0] == 10


def test_recall_and_precision_at_top_slice():
    y = np.array([1, 0, 0, 1, 0, 0, 0, 0, 0, 1])
    score = np.array([0.9, 0.8, 0.1, 0.2, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    assert recall_at(y, score, 0.2) == 1 / 3          # top 2 contains one of three readmissions
    assert precision_at(y, score, 0.2) == 0.5


def test_same_model_has_zero_lift():
    y = np.array([0, 1] * 50)
    p = np.linspace(0, 1, 100)
    assert paired_auroc_diff(y, p, p, n_boot=50)["diff"] == 0


def test_tiers():
    assert [tier(p, 0.5, 0.3) for p in (0.6, 0.4, 0.1)] == ["high", "medium", "low"]


def test_holdout_is_deterministic_and_about_20_percent():
    ids = [f"P{i:05d}" for i in range(2000)]
    first, second = [in_holdout(i) for i in ids], [in_holdout(i) for i in ids]
    assert first == second
    assert 0.17 < np.mean(first) < 0.23


def test_top_drivers_only_positive_and_sorted():
    d = top_drivers(np.array([0.1, -0.5, 0.7, 0.3]), ["a", "b", "c", "d"])
    assert [x["feature"] for x in d] == ["c", "d", "a"]
