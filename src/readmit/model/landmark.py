"""Landmark re-scoring: a fresh 30-day readmission risk at day 7, 14 and 21 after discharge.

    python -m readmit.model.landmark

At each landmark L, only patients still out of the hospital count (the standard landmark method): the model is
trained on training stays not readmitted by day L, to predict readmission between day L and day 30.
Features = everything the discharge model uses + what was learned since discharge (history-available signals):
  rx_filled, rx_days_to_fill   pharmacy claims
  ed_visits                    ED / urgent care visits, sent home
  followup_kept                a follow-up visit happened
Each landmark model is compared with a model on the same at-risk patients without the new signals, so the
difference is exactly what the post-discharge information adds. Evaluated by 5-fold cross-validation over all
past stays, grouped by patient (a single test split leaves too few readmissions after day 7 to measure). Outreach behavior (replied, silent, barriers)
is not in here yet: it has no history before the program started (cold start). It comes after the program
has run long enough to train on.
"""

import json
import warnings

import mlflow
import mlflow.xgboost
import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold

from readmit import db
from readmit.config import REPO_ROOT, settings
from readmit.model import features
from readmit.model.metrics import evaluate, paired_auroc_diff
from readmit.model.train import fit_xgb

LANDMARKS = (7, 14, 21)
SIGNALS = ["rx_filled", "rx_days_to_fill", "ed_visits", "followup_kept"]
MODEL_DIR = REPO_ROOT / "data" / "models"


def signal_features(events: pd.DataFrame, landmark: int, encounter_ids) -> pd.DataFrame:
    """What was known by day `landmark`, one row per stay. Events on or after the landmark are invisible."""
    events = events.assign(day=pd.to_numeric(events["day"], errors="coerce").astype(float))
    seen = events[events["day"] < landmark]
    out = pd.DataFrame(index=pd.Index(list(encounter_ids), name="encounter_id"))
    fills = seen[seen["type"] == "rx_fill"].groupby("encounter_id")["day"].min().astype(float)
    out["rx_filled"] = out.index.isin(fills.index).astype(int)
    out["rx_days_to_fill"] = fills.reindex(out.index).fillna(float(landmark))  # not filled yet = waited all along
    visits = seen[seen["type"] == "ed_visit"].groupby("encounter_id").size()
    out["ed_visits"] = visits.reindex(out.index).fillna(0).astype(int)
    kept = seen[seen["type"] == "followup_kept"]["encounter_id"].unique()
    out["followup_kept"] = out.index.isin(kept).astype(int)
    return out


def at_risk(stays: pd.DataFrame, landmark: int) -> pd.DataFrame:
    """Stays still out of the hospital at the landmark: no readmission yet (later readmissions still count)."""
    gap = stays["days_to_next_unplanned"]
    return stays[gap.isna() | (gap > landmark)]


def build(stays: pd.DataFrame, social: pd.DataFrame | None, events: pd.DataFrame, landmark: int) -> pd.DataFrame:
    X = features.build(stays, social)
    return X.join(signal_features(events, landmark, X.index))


def out_of_fold(X: pd.DataFrame, y: pd.Series, groups: pd.Series, folds: int = 5) -> np.ndarray:
    """Predictions for every stay from a model that never saw that patient (grouped folds: no patient is in
    both the training and the scored part). Uses all historical stays, so small landmark sets still give
    stable estimates."""
    p = np.zeros(len(X))
    for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
        p[te] = fit_xgb(X.iloc[tr], y.iloc[tr]).predict_proba(X.iloc[te])[:, 1]
    return p


def main() -> None:
    warnings.filterwarnings("ignore", category=UserWarning)  # mlflow/xgboost model-format hints
    stays = db.read("select * from cohort.stays where split in ('train', 'test')").sort_values("discharge_datetime")
    social = db.read("select * from features.social_extracted")
    events = db.read("select * from sim.post_discharge")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("readmission-landmark")

    report = {}
    print("\n  5-fold cross-validation over all past stays, folds grouped by patient")
    print(
        f"  {'landmark':<9} {'at risk':>8} {'events':>7} {'AUROC base':>11} {'AUROC +signals':>15} {'lift (95% CI)':>24}"
    )
    for L in LANDMARKS:
        risk_set = at_risk(stays, L)
        y = risk_set["readmit_30d"].astype(int).reset_index(drop=True)
        groups = risk_set["patient_id"].reset_index(drop=True)
        base = features.build(risk_set, social).reset_index(drop=True)
        full = build(risk_set, social, events, L).reset_index(drop=True)
        p_base, p_full = out_of_fold(base, y, groups), out_of_fold(full, y, groups)
        m_base, m_full = evaluate(y, p_base), evaluate(y, p_full)
        lift = paired_auroc_diff(y, p_full, p_base)
        print(
            f"  day {L:<5} {len(y):>8} {int(y.sum()):>7} {m_base['auroc']:>11.3f} {m_full['auroc']:>15.3f}"
            f" {lift['diff']:>+10.3f} [{lift['ci95'][0]:+.3f}, {lift['ci95'][1]:+.3f}]"
        )
        model = fit_xgb(full, y)  # the live model: trained on every at-risk past stay
        model.save_model(MODEL_DIR / f"landmark_d{L}.json")
        (MODEL_DIR / f"landmark_d{L}.columns.json").write_text(json.dumps(list(full.columns)))
        report[f"day_{L}"] = {"at_risk": len(y), "events": int(y.sum()), "base": m_base, "full": m_full, "lift": lift}
        with mlflow.start_run(run_name=f"landmark day {L}"):
            mlflow.log_params({"landmark_day": L, "signals": ",".join(SIGNALS), "evaluation": "5-fold grouped CV"})
            mlflow.log_metrics(
                {f"base_{k}": v for k, v in m_base.items()} | {f"full_{k}": v for k, v in m_full.items()}
            )
            mlflow.log_metric("auroc_lift", lift["diff"])
            mlflow.xgboost.log_model(model, name="model", input_example=full.head(3).astype(float))

    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "landmark_eval.json").write_text(json.dumps(report, indent=2, default=float))
    print(f"\n  saved models to {MODEL_DIR.relative_to(REPO_ROOT)}/ and the report to data/reports/landmark_eval.json")


def load_models() -> dict[int, tuple]:
    """Saved landmark models and their columns, for live re-scoring. Empty if `make landmark` hasn't run."""
    import xgboost as xgb

    models = {}
    for L in LANDMARKS:
        path = MODEL_DIR / f"landmark_d{L}.json"
        if path.exists():
            m = xgb.XGBClassifier()
            m.load_model(path)
            models[L] = (m, json.loads((MODEL_DIR / f"landmark_d{L}.columns.json").read_text()))
    return models


def live_score(model_and_cols: tuple, stay: pd.DataFrame, social: pd.DataFrame, events: pd.DataFrame, L: int) -> float:
    model, cols = model_and_cols
    X = build(stay, social, events, L).reindex(columns=cols, fill_value=0)
    return float(np.round(model.predict_proba(X)[:, 1][0], 4))


if __name__ == "__main__":
    main()
