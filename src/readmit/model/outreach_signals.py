"""Step 7b: what outreach behavior adds to the re-score, and how many cases it takes to learn it.

    make program-eval          (reads the `program` database: run `make program` first)

At day 7 and 14 after discharge, for outreach cases still out of the hospital:
  base      the discharge risk + what the hospital learns anyway (pharmacy fills, ED visits, a kept follow-up)
  +outreach the same + what the outreach program saw:
              replied      the patient answered at least once
              silent       2+ messages sent, no answer
              booked       a follow-up visit is on the calendar (or was)
              no_show      missed a booked visit
              barrier      reported a barrier (no ride, cost, ...)
              unreachable  a message bounced (wrong number, bad email)
All are the small logistic update from model/landmark.py, compared by 5-fold cross-validation grouped by patient,
on the same cases. The main test is pre-specified: only `replied` and `silent` (the engagement signals). Adding all
six on few readmissions overfits. Two measures: AUROC lift (ranking) and log-loss gain (are the probabilities
better), each with a paired bootstrap CI. In the positive control, an oracle row shows the most any engagement
signal could add.

Learning curve: the same comparison on random subsets of the cases (repeated), to show when the outreach signals
start to pay off. That is the cold-start answer: how long the program must run before retraining helps.

Outcomes: a readmission is something the hospital observes (the date and the stay), so reading which cases were
readmitted and when is fair. The outcome draws themselves (truth.outcome_draws) are never read.
"""

import json
import warnings

import mlflow
import numpy as np
import pandas as pd

from readmit import db
from readmit.config import REPO_ROOT, settings
from readmit.model.landmark import apply_update, fit_update, grouped_folds, logit, signal_features
from readmit.model.metrics import evaluate, paired_auroc_diff

LANDMARKS = (7, 14)
WORLD = ["rx_missing", "rx_late", "ed_visits", "followup_kept"]
OUTREACH = ["replied", "silent", "booked", "no_show", "barrier", "unreachable"]
CORE = ["replied", "silent"]  # pre-specified before looking at results: the two engagement signals
CURVE_SIZES = (100, 200, 400, 800)
CURVE_REPEATS = 5


def outreach_features(
    cases: pd.DataFrame, msgs: pd.DataFrame, appts: pd.DataFrame, barriers: pd.DataFrame, landmark: float
) -> pd.DataFrame:
    """What the outreach program had seen by the landmark, one row per case (index case_id).
    cases: case_id, discharge. msgs: case_id, direction, author, status, created_at.
    appts: case_id, status, start_at, created_at. barriers: case_id, at."""
    cut = cases.set_index("case_id")["discharge"] + pd.Timedelta(days=landmark)
    out = pd.DataFrame(index=cut.index)

    def before(df, col):
        return df[df[col] < df["case_id"].map(cut)]

    m = before(msgs, "created_at")
    inbound = m[m["direction"] == "inbound"].groupby("case_id").size()
    sent = m[(m["direction"] == "outbound") & (m["author"] == "ai")].groupby("case_id").size()
    out["replied"] = (inbound.reindex(out.index).fillna(0) > 0).astype(int)
    out["silent"] = ((sent.reindex(out.index).fillna(0) >= 2) & (out["replied"] == 0)).astype(int)
    out["unreachable"] = out.index.isin(m[m["status"] == "failed"]["case_id"]).astype(int)
    out["booked"] = out.index.isin(before(appts, "created_at")["case_id"]).astype(int)
    missed = before(appts, "start_at")
    out["no_show"] = out.index.isin(missed[missed["status"] == "no_show"]["case_id"]).astype(int)
    out["barrier"] = out.index.isin(before(barriers, "at")["case_id"]).astype(int)
    return out[OUTREACH]


def kept_visits(cases: pd.DataFrame, appts: pd.DataFrame) -> pd.DataFrame:
    """Completed outreach visits as followup_kept events, in days after discharge (for signal_features)."""
    done = appts[appts["status"] == "completed"].merge(cases[["case_id", "encounter_id", "discharge"]], on="case_id")
    return pd.DataFrame(
        {
            "encounter_id": done["encounter_id"],
            "day": (done["start_at"] - done["discharge"]).dt.total_seconds() / 86400,
            "type": "followup_kept",
        }
    )


def load() -> dict:
    with db.transaction() as c:
        needed = ("outreach.cases", "truth.outcome_results", "sim.post_discharge")
        if not all(db.one(c, f"select to_regclass('{t}') is not null as ok")["ok"] for t in needed):
            raise SystemExit("no program data: run `make program` first")
    cases = db.read(
        """select k.case_id, k.encounter_id, k.patient_id, s.discharge_datetime as discharge, sc.risk as prior,
                  o.readmitted, o.decided_at
           from outreach.cases k
           join cohort.stays s using (encounter_id)
           join scoring.scores sc using (encounter_id)
           join truth.outcome_results o using (encounter_id)"""
    )
    return {
        "cases": cases,
        "msgs": db.read("select case_id, direction, author, status, created_at from outreach.messages"),
        "appts": db.read("select case_id, status, start_at, created_at from outreach.appointments"),
        "barriers": db.read("select case_id, at from outreach.case_barriers"),
        "events": db.read(
            "select encounter_id, day, type from sim.post_discharge where encounter_id = any(:ids)",
            ids=cases["encounter_id"].tolist(),
        ),
    }


def frame(d: dict, landmark: float) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """At-risk cases at the landmark: features (prior_logit + world + outreach), labels, patient groups."""
    cases = d["cases"]
    cut = cases["discharge"] + pd.Timedelta(days=landmark)
    risk_set = cases[~(cases["readmitted"] & (cases["decided_at"] <= cut))].reset_index(drop=True)
    events = pd.concat([d["events"], kept_visits(risk_set, d["appts"])], ignore_index=True)
    world = signal_features(events, landmark, risk_set["encounter_id"]).reset_index(drop=True)
    outreach = outreach_features(risk_set, d["msgs"], d["appts"], d["barriers"], landmark).reset_index(drop=True)
    X = pd.concat([pd.DataFrame({"prior_logit": logit(risk_set["prior"])}), world, outreach], axis=1)
    y = risk_set["readmitted"].astype(int).to_numpy()
    return X, y, risk_set["patient_id"].to_numpy(), risk_set["case_id"].to_numpy()


def logloss_gain(y, p_new, p_old, n_boot: int = 1000, seed: int = 7) -> dict:
    """How much lower the log-loss is with the new model (positive = better), with a paired bootstrap CI.
    More sensitive than AUROC to one modest binary signal: it rewards getting the probabilities right, not
    only the ranking."""
    y = np.asarray(y).astype(int)

    def ll(p):
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))

    gain = ll(p_old) - ll(p_new)
    rng = np.random.default_rng(seed)
    boots = [gain[rng.integers(0, len(y), len(y))].mean() for _ in range(n_boot)]
    return {"diff": float(gain.mean()), "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]}


def compare(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, oracle: np.ndarray | None = None) -> dict:
    """Grouped 5-fold CV on the same cases:
    base     prior + world signals
    core     + the two pre-specified signals (replied, silent): the main test
    all      + all six outreach signals (more parameters: shows the cost of fishing)
    oracle   + the patient's TRUE extra risk from the world (positive control only, never available in practice):
             the most any engagement signal could add"""
    sets = {"base": ["prior_logit", *WORLD], "core": ["prior_logit", *WORLD, *CORE], "all": list(X.columns)}
    if oracle is not None:
        X = X.assign(true_engagement_risk=oracle)
        sets["oracle"] = ["prior_logit", *WORLD, "true_engagement_risk"]
    p = {k: np.zeros(len(y)) for k in sets}
    for tr, te in grouped_folds(len(y), groups):
        for k, cols in sets.items():
            p[k][te] = apply_update(fit_update(X.iloc[tr][cols], y[tr]), X.iloc[te][cols])
    out = {k: evaluate(y, v) for k, v in p.items()}
    for k in sets:
        if k != "base":
            out[f"lift_{k}"] = paired_auroc_diff(y, p[k], p["base"])
            out[f"logloss_gain_{k}"] = logloss_gain(y, p[k], p["base"])
    return out


def learning_curve(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, seed: int = 7) -> list[dict]:
    """The core test on random subsets (repeated): how many cases before the signals reliably help."""
    rng = np.random.default_rng(seed)
    rows = []
    for n in [s for s in CURVE_SIZES if s < len(y)] + [len(y)]:
        lifts, gains = [], []
        for _ in range(CURVE_REPEATS if n < len(y) else 1):
            idx = np.sort(rng.choice(len(y), n, replace=False))
            if y[idx].sum() < 10:  # too few readmissions to fit anything
                continue
            r = compare(X.iloc[idx].reset_index(drop=True), y[idx], groups[idx])
            lifts.append(r["lift_core"]["diff"])
            gains.append(r["logloss_gain_core"]["diff"])
        if lifts:
            rows.append(
                {
                    "cases": n,
                    "readmissions": round(float(y.mean()) * n),
                    "lift": float(np.mean(lifts)),
                    "logloss_gain": float(np.mean(gains)),
                    "gain_positive": f"{sum(g > 0 for g in gains)}/{len(gains)}",
                }
            )
    return rows


def odds_ratios(X: pd.DataFrame, y: np.ndarray) -> dict:
    m = fit_update(X, y)
    return {f: round(float(np.exp(m["coef"][f] / m["scale"][f])), 2) for f in OUTREACH}


def main() -> None:
    warnings.filterwarnings("ignore", category=UserWarning)
    d = load()
    n_cases = len(d["cases"])
    meta = (
        db.read("select * from sim.program_meta").iloc[0]
        if db.read("select to_regclass('sim.program_meta') is not null as ok").iloc[0]["ok"]
        else None
    )
    engagement = json.loads(meta["engagement_risk"]) if meta is not None else {}
    print(
        f"\n  world: {'POSITIVE CONTROL, engagement risk ' + str(engagement) if engagement else 'main rules (engagement says nothing about risk)'}"
    )
    print(f"\n  program: {n_cases} outreach cases, {int(d['cases']['readmitted'].sum())} readmitted")
    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("readmission-outreach-signals")
    oracle_by_case = None
    if engagement:
        personas = db.read("select patient_id, persona from truth.patients")
        oracle_by_case = d["cases"].merge(personas, on="patient_id", how="left").set_index("case_id")["persona"]
        oracle_by_case = oracle_by_case.map(lambda x: engagement.get(x, 0.0))
    report = {}
    for L in LANDMARKS:
        X, y, groups, case_ids = frame(d, L)
        oracle = oracle_by_case.reindex(case_ids).to_numpy() if oracle_by_case is not None else None
        r = compare(X, y, groups, oracle)
        print(f"\n  day {L}: {len(y)} at risk, {int(y.sum())} readmitted")
        print(f"    {'model':<34} {'AUROC':>6} {'lift vs base (95% CI)':>27} {'log-loss gain (95% CI)':>32}")
        print(f"    {'base: prior + world signals':<34} {r['base']['auroc']:>6.3f}")
        names = {
            "core": "+ replied, silent (pre-specified)",
            "all": "+ all 6 outreach signals",
            "oracle": "+ TRUE engagement risk (oracle)",
        }
        for k, name in names.items():
            if f"lift_{k}" not in r:
                continue
            lift, gain = r[f"lift_{k}"], r[f"logloss_gain_{k}"]
            print(
                f"    {name:<34} {r[k]['auroc']:>6.3f} {lift['diff']:>+9.3f} [{lift['ci95'][0]:+.3f}, {lift['ci95'][1]:+.3f}]"
                f" {gain['diff']:>+11.4f} [{gain['ci95'][0]:+.4f}, {gain['ci95'][1]:+.4f}]"
            )
        ors = odds_ratios(X, y)
        print(f"    odds ratios (all 6): {ors}")
        curve = learning_curve(X, y, groups)
        print("    learning curve, pre-specified signals (cases: AUROC lift, log-loss gain, runs with gain > 0):")
        for c in curve:
            print(
                f"      {c['cases']:>5} cases (~{c['readmissions']} readmissions): {c['lift']:+.3f}, "
                f"{c['logloss_gain']:+.4f}, {c['gain_positive']}"
            )
        report[f"day_{L}"] = {"at_risk": len(y), "events": int(y.sum()), **r, "odds_ratios": ors, "curve": curve}
        with mlflow.start_run(run_name=f"outreach signals day {L}"):
            mlflow.log_params({"landmark_day": L, "cases": n_cases, "control": bool(engagement)})
            mlflow.log_metrics(
                {f"{k}_auroc": r[k]["auroc"] for k in ("base", *names) if k in r}
                | {f"logloss_gain_{k}": r[f"logloss_gain_{k}"]["diff"] for k in names if f"logloss_gain_{k}" in r}
            )
            mlflow.log_dict({"odds_ratios": ors, "curve": curve}, f"outreach_signals_d{L}.json")

    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "outreach_signals_eval.json").write_text(json.dumps(report, indent=2, default=float))
    print("\n  saved the report to data/reports/outreach_signals_eval.json")


if __name__ == "__main__":
    main()
