"""Score the extraction against the answer key, and report what it cost.

    python -m readmit.extract.evaluate

Answer key = truth.documented: which factors were actually written in each stay's notes before the
prediction cutoff. (The extractor can only be expected to find what was written.)
Logs metrics to MLflow (experiment "note-extraction") and writes data/reports/extraction_eval.json.
"""
import json

import pandas as pd

from readmit.config import REPO_ROOT, settings
from readmit.sim.notes import SOCIAL_FACTORS


def score(pred: pd.DataFrame, truth: pd.DataFrame) -> dict:
    """Precision / recall / F1 per factor, plus micro-averaged overall."""
    df = pred[pred["extracted"]].merge(truth, on="encounter_id", suffixes=("_pred", "_true"))
    per, tp_all, fp_all, fn_all = {}, 0, 0, 0
    for f in SOCIAL_FACTORS:
        p, t = df[f"{f}_pred"].astype(bool), df[f"{f}_true"].astype(bool)
        tp, fp, fn = int((p & t).sum()), int((p & ~t).sum()), int((~p & t).sum())
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
        per[f] = {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1}
        tp_all, fp_all, fn_all = tp_all + tp, fp_all + fp, fn_all + fn
    prec = tp_all / (tp_all + fp_all) if tp_all + fp_all else None
    rec = tp_all / (tp_all + fn_all) if tp_all + fn_all else None
    overall = {"precision": prec, "recall": rec, "f1": 2 * prec * rec / (prec + rec) if prec and rec else None,
               "stays_scored": len(df)}
    return {"overall": overall, "per_factor": per}


def usage_summary() -> dict:
    from readmit import db
    u = db.read("""select count(*) as calls,
                          sum(case when cached then 1 else 0 end) as cached_calls,
                          sum(case when not ok then 1 else 0 end) as failed_calls,
                          sum(case when repaired then 1 else 0 end) as repaired_calls,
                          coalesce(sum(input_tokens), 0) as input_tokens,
                          coalesce(sum(output_tokens), 0) as output_tokens,
                          sum(cost_usd) as cost_usd,
                          percentile_cont(0.5) within group (order by latency_ms)
                              filter (where not cached and ok) as p50_ms,
                          percentile_cont(0.95) within group (order by latency_ms)
                              filter (where not cached and ok) as p95_ms
                   from ops.llm_calls where task = 'extract_social'""").iloc[0]
    return {k: (None if pd.isna(v) else float(v)) for k, v in u.items()}


def main():
    import mlflow

    from readmit import db
    from readmit.extract.social import PROMPT_VERSION

    pred = db.read("select * from features.social_extracted")
    truth = db.read("select * from truth.documented")
    result = score(pred, truth)
    result["usage"] = usage_summary()
    result["prompt_version"] = PROMPT_VERSION

    print(f"\n  {'factor':<20} {'precision':>9} {'recall':>7} {'f1':>6}   tp  fp  fn")
    for f, m in result["per_factor"].items():
        def fmt(x):
            return f"{x:.2f}" if x is not None else "  - "
        print(f"  {f:<20} {fmt(m['precision']):>9} {fmt(m['recall']):>7} {fmt(m['f1']):>6}"
              f"  {m['tp']:>3} {m['fp']:>3} {m['fn']:>3}")
    o = result["overall"]
    print(f"\n  overall: precision {o['precision']:.3f}, recall {o['recall']:.3f}, f1 {o['f1']:.3f} "
          f"on {o['stays_scored']} stays")
    print(f"  usage: {json.dumps(result['usage'])}")

    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "extraction_eval.json").write_text(json.dumps(result, indent=2))

    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("note-extraction")
    with mlflow.start_run(run_name=f"extract_social_{PROMPT_VERSION}"):
        mlflow.log_params({"prompt_version": PROMPT_VERSION, "model": settings.llm_model,
                           "stays_scored": o["stays_scored"]})
        mlflow.log_metrics({"precision": o["precision"] or 0, "recall": o["recall"] or 0, "f1": o["f1"] or 0})
        for f, m in result["per_factor"].items():
            if m["f1"] is not None:
                mlflow.log_metric(f"f1_{f}", m["f1"])
        for k, v in result["usage"].items():
            if v is not None:
                mlflow.log_metric(f"usage_{k}", v)
        mlflow.log_artifact(str(out / "extraction_eval.json"))


if __name__ == "__main__":
    main()
