"""Score live discharges with the champion model: risk, tier, top reasons, holdout.

    python -m readmit.model.score

Writes scoring.scores: one row per live stay. The conversation-loop milestone reads it day by day
(each stay becomes visible at its prediction time = discharge + 24h).
"""
import hashlib
import json

import mlflow
import mlflow.xgboost
import numpy as np
import pandas as pd
import xgboost as xgb

from readmit import db
from readmit.config import settings
from readmit.model import features
from readmit.sim.notes import SOCIAL_FACTORS

HOLDOUT_PCT = 20


def tier(p: float, cut_high: float, cut_medium: float) -> str:
    return "high" if p >= cut_high else "medium" if p >= cut_medium else "low"


def in_holdout(patient_id: str, pct: int = HOLDOUT_PCT) -> bool:
    """Deterministic: the same patient always lands in the same group, run after run."""
    return int(hashlib.sha256(patient_id.encode()).hexdigest(), 16) % 100 < pct


def top_drivers(contribs: np.ndarray, columns: list[str], n: int = 3) -> list[dict]:
    """Largest positive SHAP contributions = the reasons this patient's risk is high."""
    order = np.argsort(-contribs)[:n]
    return [{"feature": columns[i], "contribution": round(float(contribs[i]), 3)} for i in order if contribs[i] > 0]


def main():
    meta = db.read("select * from scoring.model_meta").iloc[0]
    cols = json.loads(meta["feature_columns"])
    mlflow.set_tracking_uri(settings.mlflow_uri)
    model = mlflow.xgboost.load_model(f"models:/{meta['model_name']}@champion")

    live = db.read("select * from cohort.stays where split = 'live' order by discharge_datetime")
    social = db.read("select * from features.social_extracted")
    X = features.build(live, social).reindex(columns=cols, fill_value=0)

    prob = model.predict_proba(X)[:, 1]
    contribs = model.get_booster().predict(xgb.DMatrix(X), pred_contribs=True)[:, :-1]   # last column = bias

    soc = social.set_index("encounter_id")
    rows = []
    for i, r in enumerate(live.itertuples()):
        t = tier(prob[i], meta["tier_cut_high"], meta["tier_cut_medium"])
        barriers = [f for f in SOCIAL_FACTORS if r.encounter_id in soc.index and bool(soc.at[r.encounter_id, f])]
        rows.append({
            "encounter_id": r.encounter_id, "patient_id": r.patient_id,
            "discharge_datetime": r.discharge_datetime, "prediction_time": r.prediction_time,
            "risk": round(float(prob[i]), 4), "tier": t,
            "flagged": t in ("high", "medium"),
            "holdout": t in ("high", "medium") and in_holdout(r.patient_id),
            "top_drivers": json.dumps(top_drivers(contribs[i], cols)),
            "barriers": json.dumps(barriers),
            "model_version": meta["model_version"],
        })
    scores = pd.DataFrame(rows)
    db.write(scores, "scoring.scores")

    flagged = scores[scores["flagged"]]
    print(f"  scored {len(scores)} live stays with {meta['model_name']} v{meta['model_version']}")
    print(f"  tiers: {scores['tier'].value_counts().to_dict()}")
    print(f"  flagged {len(flagged)}: {int((~flagged['holdout']).sum())} to outreach, "
          f"{int(flagged['holdout'].sum())} holdout")
    ex = flagged.sort_values("risk", ascending=False).iloc[0]
    print(f"  highest risk: {ex['encounter_id']} risk {ex['risk']:.2f}, drivers {ex['top_drivers']}, "
          f"barriers {ex['barriers']}")


if __name__ == "__main__":
    main()
