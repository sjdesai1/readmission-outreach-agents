"""Train and compare readmission models; register the champion in MLflow.

    python -m readmit.model.train

Compares on the TEST split (later in time than training):
  LACE score (clinical baseline) | logistic regression | XGBoost x 3 feature sets | true-risk ceiling
Registers "structured+extracted" XGBoost as readmission-risk@champion, with tier cut points
fixed from the test-set score distribution (high = top 10%, medium = next 20%).
"""

import json

import mlflow
import mlflow.xgboost
import numpy as np
import pandas as pd
import xgboost as xgb
from mlflow.tracking import MlflowClient
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from readmit import db
from readmit.config import REPO_ROOT, settings
from readmit.model import features
from readmit.model.metrics import calibration_table, evaluate, paired_auroc_diff

MODEL_NAME = "readmission-risk"
CHAMPION_VARIANT = "structured+extracted"
XGB_PARAMS = {
    "n_estimators": 600,
    "learning_rate": 0.03,
    "max_depth": 3,
    "min_child_weight": 5,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "eval_metric": "logloss",
    "early_stopping_rounds": 50,
    "random_state": 7,
}
TIER_FRACTIONS = {"high": 0.10, "medium": 0.30}  # cumulative: top 10% high, next 20% medium


def load():
    stays = db.read("select * from cohort.stays where split in ('train', 'test')")
    extracted = db.read("select * from features.social_extracted")
    documented = db.read("select * from truth.documented")
    truth = db.read("select encounter_id, true_risk from truth.encounters")
    return stays, extracted, documented, truth


def fit_xgb(X_tr, y_tr):
    """Hold out the latest 20% of training stays (by time) for early stopping."""
    cut = int(len(X_tr) * 0.8)
    model = xgb.XGBClassifier(**XGB_PARAMS)
    model.fit(X_tr.iloc[:cut], y_tr.iloc[:cut], eval_set=[(X_tr.iloc[cut:], y_tr.iloc[cut:])], verbose=False)
    return model


def main():
    stays, extracted, documented, truth = load()
    stays = stays.sort_values("discharge_datetime")
    train, test = stays[stays["split"] == "train"], stays[stays["split"] == "test"]
    y_tr, y_te = train["readmit_30d"].astype(int), test["readmit_30d"].astype(int)
    social = {"extracted": extracted, "true": documented}

    results, models, frames = {}, {}, {}
    # clinical baseline: LACE needs no training
    results["LACE score"] = evaluate(y_te, features.lace(test).to_numpy(), prob=False)

    # simple ML baseline
    X_tr, X_te = features.build(train), features.build(test)
    logreg = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(max_iter=2000))
    logreg.fit(X_tr, y_tr)
    results["logistic (structured)"] = evaluate(y_te, logreg.predict_proba(X_te)[:, 1])

    for variant, source in features.VARIANTS.items():
        s = social.get(source)
        X_tr, X_te = features.build(train, s), features.build(test, s)
        model = fit_xgb(X_tr, y_tr)
        p = model.predict_proba(X_te)[:, 1]
        results[f"xgboost ({variant})"] = evaluate(y_te, p)
        models[variant], frames[variant] = model, (X_tr, X_te, p)

    ceiling = test.merge(truth, on="encounter_id")
    results["ceiling (true risk)"] = evaluate(ceiling["readmit_30d"].astype(int), ceiling["true_risk"])

    # champion details: by patient group, calibration, tier cut points
    X_tr, X_te, p = frames[CHAMPION_VARIANT]
    champ = test.assign(p=p)
    by_group = {
        g: evaluate(d["readmit_30d"].astype(int), d["p"])
        for g, d in champ.groupby("patient_status")
        if d["readmit_30d"].nunique() == 2
    }
    cuts = {t: float(np.quantile(p, 1 - f)) for t, f in TIER_FRACTIONS.items()}
    # do the LLM-extracted note features really help, or is the difference noise?
    notes_lift = paired_auroc_diff(y_te, frames["structured+extracted"][2], frames["structured"][2])
    calib = calibration_table(y_te, p)

    print(f"\n  {'model':<32} {'AUROC':>6} {'AUPRC':>6} {'recall@10%':>10} {'recall@30%':>10}")
    for name, m in results.items():
        print(
            f"  {name:<32} {m['auroc']:>6.3f} {m['auprc']:>6.3f} {m['recall_top10']:>10.2f} {m['recall_top30']:>10.2f}"
        )
    print("\n  champion by patient group: " + ", ".join(f"{g} AUROC {m['auroc']:.3f}" for g, m in by_group.items()))
    print(f"  calibration (predicted vs observed by quintile): {calib}")
    print(
        f"  notes lift (AUROC, extracted - structured): {notes_lift['diff']:+.3f}, "
        f"95% CI [{notes_lift['ci95'][0]:+.3f}, {notes_lift['ci95'][1]:+.3f}]"
    )
    print(f"  tier cut points: high >= {cuts['high']:.3f}, medium >= {cuts['medium']:.3f}")

    report = {
        "results": results,
        "champion_variant": CHAMPION_VARIANT,
        "by_patient_group": by_group,
        "calibration": calib,
        "tier_cuts": cuts,
        "notes_lift": notes_lift,
        "feature_columns": list(X_tr.columns),
        "train_stays": len(train),
        "test_stays": len(test),
    }
    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_eval.json").write_text(json.dumps(report, indent=2))

    # ---- MLflow: one run per model; champion registered with an alias -------------------------
    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("readmission-model")
    for name, m in results.items():
        with mlflow.start_run(run_name=name):
            mlflow.log_metrics(m)
    with mlflow.start_run(run_name=f"champion: xgboost ({CHAMPION_VARIANT})") as run:
        mlflow.log_params(
            {k: v for k, v in XGB_PARAMS.items()}
            | {"variant": CHAMPION_VARIANT, "best_iteration": int(models[CHAMPION_VARIANT].best_iteration)}
        )
        mlflow.log_metrics(results[f"xgboost ({CHAMPION_VARIANT})"])
        mlflow.log_metrics({f"tier_cut_{t}": c for t, c in cuts.items()})
        mlflow.log_artifact(str(out / "model_eval.json"))
        info = mlflow.xgboost.log_model(
            models[CHAMPION_VARIANT], name="model", registered_model_name=MODEL_NAME, input_example=X_tr.head(3)
        )
    version = getattr(info, "registered_model_version", None) or max(
        int(v.version) for v in MlflowClient().search_model_versions(f"name='{MODEL_NAME}'")
    )
    MlflowClient().set_registered_model_alias(MODEL_NAME, "champion", version)

    # what the scorer needs, next to the scores it produces
    meta = pd.DataFrame(
        [
            {
                "model_name": MODEL_NAME,
                "model_version": str(version),
                "run_id": run.info.run_id,
                "variant": CHAMPION_VARIANT,
                "tier_cut_high": cuts["high"],
                "tier_cut_medium": cuts["medium"],
                "feature_columns": json.dumps(list(X_tr.columns)),
            }
        ]
    )
    db.write(meta, "scoring.model_meta")
    print(f"\n  registered {MODEL_NAME} version {version} as @champion")


if __name__ == "__main__":
    main()
