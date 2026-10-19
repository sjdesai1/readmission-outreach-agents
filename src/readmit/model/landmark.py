"""Re-scoring after discharge: update the discharge risk with what was learned since.

    python -m readmit.model.landmark

Two ways a case gets re-scored (both in the simulation, sim/rescore.py):
  landmarks   day 7, 14 and 21 after discharge: a safety net on a fixed schedule
  events      right away when something happens: an ED visit (sent home), or no prescription fill by day 3

The update model is small on purpose: a logistic regression on the discharge risk (as a log-odds "prior") plus
the new signals. The first version refit the whole XGBoost model from the few readmissions left after each
landmark and overfit: the signals made it worse (day 14: -0.022 AUROC, 95% CI excluding zero). Updating the prior
needs 5 parameters, not hundreds.

Signals available for past stays (so the models can be trained now):
  rx_missing      no discharge prescription filled yet (pharmacy claims)
  rx_late         filled, but after day 3
  ed_visits       ED / urgent care visits, sent home
  followup_kept   a follow-up visit happened
The fill signals are yes/no on purpose. A first version used "days to fill", censored at the landmark: the same
value meant different things on different days, and a day-7 model applied on day 3 read a missed fill as good news.

Models are trained for day 3 (used by the missed-fill re-score) and days 7, 14, 21 (the safety net).
Outreach behavior (replied, silent, barriers) has no history before the program started: it is added after a cold
start (step 7b).

Evaluation: 5-fold cross-validation over all past stays, grouped by patient. At each landmark only patients still
out of the hospital count (the landmark method). The prior alone vs prior + signals, on the same patients.
"""

import json
import warnings

import mlflow
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

from readmit import db
from readmit.config import REPO_ROOT, settings
from readmit.model import features
from readmit.model.metrics import evaluate, paired_auroc_diff
from readmit.model.train import fit_xgb

LANDMARKS = (7, 14, 21)  # scheduled re-scores
TRAIN_DAYS = (3, *LANDMARKS)  # update models: day 3 also serves event re-scores early on
FILL_GRACE = 3.0  # a fill after this many days is late
SIGNALS = ["rx_missing", "rx_late", "ed_visits", "followup_kept"]
UPDATE_FEATURES = ["prior_logit", *SIGNALS]
MODEL_DIR = REPO_ROOT / "data" / "models"


# ------------------------------------------------------------------ features
def signal_features(events: pd.DataFrame, landmark: float, encounter_ids) -> pd.DataFrame:
    """What was known by day `landmark`, one row per stay. Events on or after it are invisible."""
    events = events.assign(day=pd.to_numeric(events["day"], errors="coerce").astype(float))
    seen = events[events["day"] < landmark]
    out = pd.DataFrame(index=pd.Index(list(encounter_ids), name="encounter_id"))
    fills = seen[seen["type"] == "rx_fill"].groupby("encounter_id")["day"].min().astype(float)
    first_fill = fills.reindex(out.index)
    out["rx_missing"] = first_fill.isna().astype(int)
    out["rx_late"] = (first_fill >= FILL_GRACE).astype(int)
    visits = seen[seen["type"] == "ed_visit"].groupby("encounter_id").size()
    out["ed_visits"] = visits.reindex(out.index).fillna(0).astype(int)
    kept = seen[seen["type"] == "followup_kept"]["encounter_id"].unique()
    out["followup_kept"] = out.index.isin(kept).astype(int)
    return out


def at_risk(stays: pd.DataFrame, landmark: float) -> pd.DataFrame:
    """Stays still out of the hospital at the landmark: no readmission yet (later readmissions still count)."""
    gap = stays["days_to_next_unplanned"]
    return stays[gap.isna() | (gap > landmark)]


def logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def update_frame(prior: pd.Series, events: pd.DataFrame, landmark: float) -> pd.DataFrame:
    """prior: discharge risk by encounter_id. Returns the update model's inputs, one row per stay."""
    X = signal_features(events, landmark, prior.index)
    X.insert(0, "prior_logit", logit(prior.to_numpy()))
    return X[UPDATE_FEATURES]


# ------------------------------------------------------------------ the update model (plain numbers, saved as JSON)
def fit_update(X: pd.DataFrame, y) -> dict:
    mean, scale = X.mean(), X.std().fillna(1.0).replace(0, 1.0)
    lr = LogisticRegression(C=1.0, max_iter=1000).fit((X - mean) / scale, np.asarray(y, dtype=int))
    return {
        "features": list(X.columns),
        "mean": {k: float(v) for k, v in mean.items()},
        "scale": {k: float(v) for k, v in scale.items()},
        "coef": dict(zip(X.columns, lr.coef_[0].tolist(), strict=True)),
        "intercept": float(lr.intercept_[0]),
    }


def apply_update(model: dict, X: pd.DataFrame) -> np.ndarray:
    z = model["intercept"] + sum(
        model["coef"][f] * (X[f].to_numpy(dtype=float) - model["mean"][f]) / model["scale"][f]
        for f in model["features"]
    )
    return 1 / (1 + np.exp(-z))


def odds_ratios(model: dict) -> dict:
    """Per one unit of each signal (e.g. one ED visit), how the odds of readmission change."""
    return {f: round(float(np.exp(model["coef"][f] / model["scale"][f])), 2) for f in SIGNALS}


# ------------------------------------------------------------------ training and evaluation
def grouped_folds(n: int, groups, folds: int = 5):
    return GroupKFold(n_splits=folds).split(np.zeros(n), groups=groups)


def discharge_prior(stays: pd.DataFrame, social: pd.DataFrame) -> pd.Series:
    """Out-of-fold discharge risk for every past stay (the same kind of model as the champion), so the
    update model learns from priors it would really have had."""
    X, y = features.build(stays, social), stays["readmit_30d"].astype(int)
    p = np.zeros(len(X))
    for tr, te in grouped_folds(len(X), stays["patient_id"].to_numpy()):
        p[te] = fit_xgb(X.iloc[tr], y.iloc[tr]).predict_proba(X.iloc[te])[:, 1]
    return pd.Series(p, index=stays["encounter_id"].to_numpy(), name="prior")


def main() -> None:
    warnings.filterwarnings("ignore", category=UserWarning)
    stays = db.read("select * from cohort.stays where split in ('train', 'test')").sort_values("discharge_datetime")
    social = db.read("select * from features.social_extracted")
    events = db.read("select * from sim.post_discharge")
    prior = discharge_prior(stays, social)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for old in MODEL_DIR.glob("landmark_d*.columns.json"):  # the old XGBoost version's files
        old.unlink()
    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("readmission-landmark")

    report = {}
    print("\n  5-fold cross-validation over all past stays, folds grouped by patient")
    print(f"  {'landmark':<9} {'at risk':>8} {'events':>7} {'AUROC prior':>12} {'+signals':>9} {'lift (95% CI)':>24}")
    for L in TRAIN_DAYS:
        risk_set = at_risk(stays, L)
        y = risk_set["readmit_30d"].astype(int).to_numpy()
        p_prior = prior.loc[risk_set["encounter_id"]]
        X = update_frame(p_prior, events, L)
        p_full = np.zeros(len(X))
        for tr, te in grouped_folds(len(X), risk_set["patient_id"].to_numpy()):
            p_full[te] = apply_update(fit_update(X.iloc[tr], y[tr]), X.iloc[te])
        m_prior, m_full = evaluate(y, p_prior.to_numpy()), evaluate(y, p_full)
        lift = paired_auroc_diff(y, p_full, p_prior.to_numpy())
        model = fit_update(X, y)  # the live model: every at-risk past stay
        ors = odds_ratios(model)
        print(
            f"  day {L:<5} {len(y):>8} {int(y.sum()):>7} {m_prior['auroc']:>12.3f} {m_full['auroc']:>9.3f}"
            f" {lift['diff']:>+10.3f} [{lift['ci95'][0]:+.3f}, {lift['ci95'][1]:+.3f}]"
        )
        print(f"            odds ratios: {ors}")
        (MODEL_DIR / f"landmark_d{L}.json").write_text(json.dumps(model, indent=2))
        report[f"day_{L}"] = {
            "at_risk": len(y),
            "events": int(y.sum()),
            "prior": m_prior,
            "updated": m_full,
            "lift": lift,
            "odds_ratios": ors,
        }
        with mlflow.start_run(run_name=f"landmark day {L}"):
            mlflow.log_params({"landmark_day": L, "model": "logistic update of the discharge prior"})
            mlflow.log_metrics(
                {f"prior_{k}": v for k, v in m_prior.items()} | {f"updated_{k}": v for k, v in m_full.items()}
            )
            mlflow.log_metric("auroc_lift", lift["diff"])
            mlflow.log_dict(model, f"landmark_d{L}.json")

    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "landmark_eval.json").write_text(json.dumps(report, indent=2, default=float))
    print(f"\n  saved models to {MODEL_DIR.relative_to(REPO_ROOT)}/ and the report to data/reports/landmark_eval.json")


# ------------------------------------------------------------------ live use
def load_models() -> dict[int, dict]:
    """Saved update models by landmark day. Empty if `make landmark` hasn't run (the simulation then skips)."""
    models = {}
    for L in TRAIN_DAYS:
        path = MODEL_DIR / f"landmark_d{L}.json"
        if path.exists():
            try:
                m = json.loads(path.read_text())
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("features") == UPDATE_FEATURES:  # skip files from older versions
                models[L] = m
    return models


def nearest(models: dict, day: float) -> dict:
    """The update model for the landmark closest to `day` (event re-scores happen on any day)."""
    return models[min(models, key=lambda L: abs(L - day))]


def live_score(model: dict, encounter_id: str, prior_risk: float, events: pd.DataFrame, day: float) -> float:
    X = update_frame(pd.Series({encounter_id: prior_risk}), events, day)
    return float(np.round(apply_update(model, X)[0], 4))


if __name__ == "__main__":
    main()
