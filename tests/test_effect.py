"""Effect estimators graded against a known truth (pure, no database)."""

import numpy as np

from readmit.evals.effect import adjusted_effect, diff_in_rates, evaluate, n_per_arm


def world(n=2000, seed=0):
    rng = np.random.default_rng(seed)
    p0 = rng.uniform(0.1, 0.5, n)
    holdout = rng.random(n) < 0.25
    attended = rng.random(n) < np.where(p0 < 0.3, 0.8, 0.3)  # healthier patients show up more: self-selection
    return {"holdout": holdout, "p0": p0, "p1": p0 * 0.7, "attended": attended, "risk": p0}


def test_randomized_comparison_is_honest_and_the_naive_one_is_not():
    r = evaluate(**world(), draws=300)
    assert abs(r["randomized"]["bias"]) < 0.01 and r["randomized"]["coverage"] > 0.9
    assert r["naive"]["bias"] > 0.05 and r["naive"]["coverage"] < 0.2  # attenders were healthier to begin with


def test_adjusting_for_risk_removes_a_chance_imbalance():
    w = world()
    w["p0"] = np.where(w["holdout"], w["p0"] + 0.05, w["p0"])  # holdout happened to be sicker
    w["risk"] = w["p0"]
    w["p1"] = w["p0"] * 0.7
    r = evaluate(**w, draws=200)
    assert r["baseline_imbalance"] > 0.04
    assert abs(r["adjusted"]["bias"]) < abs(r["randomized"]["bias"])


def test_interval_helpers():
    d, lo, hi = diff_in_rates(np.array([1, 1, 0, 0]), np.array([0, 0, 0, 1]))
    assert d == 0.25 and lo < d < hi
    y = np.array([1, 0, 1, 0, 0, 0, 1, 0] * 50)
    arm = np.array([0, 0, 0, 0, 1, 1, 1, 1] * 50)
    est, lo, hi = adjusted_effect(y, arm, np.zeros(len(y)) + np.arange(len(y)) % 3)
    assert lo < est < hi


def test_sample_size_grows_as_the_effect_shrinks():
    assert n_per_arm(0.25, 0.08) < n_per_arm(0.25, 0.04) < n_per_arm(0.25, 0.02)
    assert n_per_arm(0.25, 0.0) == 0
