"""Metrics that match how the model is used: a care team can only call the top slice of patients."""

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score


def recall_at(y, score, frac):
    """Of everyone actually readmitted, the share that falls in the top `frac` of risk scores."""
    y, score = np.asarray(y), np.asarray(score)
    k = max(1, round(frac * len(score)))
    top = np.argsort(-score)[:k]
    return float(y[top].sum() / max(1, y.sum()))


def precision_at(y, score, frac):
    """Of the patients in the top `frac`, the share actually readmitted (how many calls are 'useful')."""
    y, score = np.asarray(y), np.asarray(score)
    k = max(1, round(frac * len(score)))
    return float(y[np.argsort(-score)[:k]].mean())


def evaluate(y, score, prob=True) -> dict:
    y = np.asarray(y).astype(int)
    out = {
        "auroc": float(roc_auc_score(y, score)),
        "auprc": float(average_precision_score(y, score)),
        "recall_top10": recall_at(y, score, 0.10),
        "recall_top30": recall_at(y, score, 0.30),
        "precision_top10": precision_at(y, score, 0.10),
        "base_rate": float(y.mean()),
    }
    if prob:
        out["brier"] = float(brier_score_loss(y, score))
    return out


def calibration_table(y, prob, bins=5) -> list[dict]:
    """Predicted vs observed readmission rate by risk quintile: does '20% risk' mean 20%?"""
    y, prob = np.asarray(y), np.asarray(prob)
    order = np.argsort(prob)
    rows = []
    for chunk in np.array_split(order, bins):
        rows.append(
            {
                "predicted": round(float(prob[chunk].mean()), 3),
                "observed": round(float(y[chunk].mean()), 3),
                "n": len(chunk),
            }
        )
    return rows


def paired_auroc_diff(y, p_a, p_b, n_boot=1000, seed=7) -> dict:
    """Is model A really better than model B, or is it noise? Bootstrap the test set, same resamples for both."""
    y, p_a, p_b = np.asarray(y).astype(int), np.asarray(p_a), np.asarray(p_b)
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max():
            continue
        diffs.append(roc_auc_score(y[i], p_a[i]) - roc_auc_score(y[i], p_b[i]))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": float(roc_auc_score(y, p_a) - roc_auc_score(y, p_b)), "ci95": [float(lo), float(hi)]}
