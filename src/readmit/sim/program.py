"""A bigger outreach program in its own database, to learn what outreach behavior says about readmission risk.

    make program                     # build the program world (20,000 patients), then run 4 months of outreach
    make program-eval                # what outreach signals add to the re-score (sim/program_eval.py)

Why a separate run: outreach signals (replied, silent, booked, no-show, barrier) only exist after the program
starts. The main demo has 46 cases and about 9 readmissions: too few to learn from (the cold start). This run
has about 20x the patients, so the question can be answered, along with how many cases it takes.

Everything goes to the database `program` (APP_DB_NAME=program), so the demo database is untouched:
  1. world        a new hospital (seed 11), same rules as the main one, 20x the patients
  2. cohort       same labels and splits
  3. extraction   simulated: the true note facts with errors at the rates the real LLM extractor measured
                  (data/reports/extraction_eval.json), so no LLM calls and no cost
  4. score        the main champion model (from MLflow) scores the live stays, same tiers and holdout
  5. outreach     the clinic calendar has more open slots (more patients); cases open as usual
  6. simulate     4 months, rule-based agents, MCP servers in-process (no Docker services needed but Postgres)
"""

import argparse
import asyncio
import dataclasses
import json

import numpy as np
import pandas as pd
import sqlalchemy as sa

from readmit import db
from readmit.config import REPO_ROOT, settings
from readmit.sim.notes import SOCIAL_FACTORS

PROGRAM_DB = "program"
SEED = 11
PATIENTS = 20_000
DAYS = 121  # Sep 1 to Dec 30: every live discharge reaches its 30-day outcome
TAKEN_BY_OTHERS = 0.30
DEFAULT_RATES = {"recall": 0.90, "false_positive_rate": 0.01}
# Positive control (`make program control=1`): patients who disengage carry real extra risk, as reported for
# post-discharge outreach in practice. The main world has no such link, so outreach signals can't help there; with
# it, the evaluation should find a lift. If it doesn't, the pipeline is broken, not the idea.
CONTROL_ENGAGEMENT_RISK = {"silent": 0.8, "opt_out": 0.4, "busy": 0.2, "engaged": -0.2}


def _engine(name: str) -> sa.Engine:
    return sa.create_engine(dataclasses.replace(settings, db_name=name).db_url)


def ensure_database() -> None:
    admin = sa.create_engine(settings.db_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            if not c.execute(sa.text("select 1 from pg_database where datname = :d"), {"d": settings.db_name}).first():
                c.execute(sa.text(f'create database "{settings.db_name}"'))
    finally:
        admin.dispose()


def extraction_rates() -> dict[str, dict]:
    """Per factor: how often the real extractor finds a documented factor (recall) and flags one that isn't
    there (false positive rate). From the last `make extract-eval`; defaults if it hasn't run."""
    path = REPO_ROOT / "data" / "reports" / "extraction_eval.json"
    if not path.exists():
        return {f: DEFAULT_RATES for f in SOCIAL_FACTORS}
    report = json.loads(path.read_text())
    n = report["overall"]["stays_scored"]
    out = {}
    for f in SOCIAL_FACTORS:
        m = report["per_factor"][f]
        pos, neg = m["tp"] + m["fn"], n - m["tp"] - m["fn"]
        out[f] = {
            "recall": m["tp"] / pos if pos else DEFAULT_RATES["recall"],
            "false_positive_rate": m["fp"] / neg if neg else DEFAULT_RATES["false_positive_rate"],
        }
    return out


def simulated_extraction(documented: pd.DataFrame, rates: dict, seed: int = SEED) -> pd.DataFrame:
    """What the extractor would have returned: documented factors found at its recall, absent ones flagged at
    its false positive rate."""
    rng = np.random.default_rng(seed)
    out = pd.DataFrame({"encounter_id": documented["encounter_id"]})
    for f in SOCIAL_FACTORS:
        truth = documented[f].astype(bool).to_numpy()
        hit = rng.random(len(out))
        out[f] = np.where(truth, hit < rates[f]["recall"], hit < rates[f]["false_positive_rate"])
    out["extracted"] = True
    out["ungrounded"] = 0
    out["evidence"] = "{}"
    out["prompt_version"] = "simulated"
    return out


def copy_champion_meta() -> None:
    """The champion's feature list and tier cut-offs, from the main database (the model itself is in MLflow)."""
    main = _engine("app")
    try:
        meta = pd.read_sql("select * from scoring.model_meta", main)
    finally:
        main.dispose()
    db.write(meta, "scoring.model_meta")


def build(patients: int = PATIENTS, seed: int = SEED, control: bool = False) -> None:
    from readmit.data import cohort
    from readmit.model import score
    from readmit.outreach import setup
    from readmit.sim.world import World

    if settings.db_name != PROGRAM_DB:
        raise SystemExit(f"run with APP_DB_NAME={PROGRAM_DB} (use `make program`), not against {settings.db_name}")
    ensure_database()
    db.ensure_schemas()
    engagement = CONTROL_ENGAGEMENT_RISK if control else {}
    print(f"  1/5 world: {patients} patients, seed {seed}, engagement risk {engagement or 'none'}")
    for name, df in World(patients, seed, engagement).build().items():
        db.write(df, name)
    db.write(
        pd.DataFrame([{"patients": patients, "seed": seed, "engagement_risk": json.dumps(engagement)}]),
        "sim.program_meta",
    )
    print("  2/5 cohort")
    cohort.main()
    print("  3/5 simulated extraction (no LLM)")
    db.write(
        simulated_extraction(db.read("select * from truth.documented"), extraction_rates(), seed),
        "features.social_extracted",
    )
    print("  4/5 score with the champion")
    copy_champion_meta()
    score.main()
    print("  5/5 outreach cases")
    setup.main(taken_by_others=TAKEN_BY_OTHERS)


def run(days: int = DAYS) -> None:
    from readmit.agents.gateway import Gateway
    from readmit.sim import days as sim_days

    if settings.db_name != PROGRAM_DB:
        raise SystemExit(f"run with APP_DB_NAME={PROGRAM_DB} (use `make program`)")
    daily = asyncio.run(sim_days.simulate(Gateway.in_memory(), days, progress=True))
    total: dict[str, int] = {}
    for d in daily:
        for k, v in d.items():
            if k != "day":
                total[k] = total.get(k, 0) + v
    print("  totals over the run: " + ", ".join(f"{k} {v}" for k, v in sorted(total.items())))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["build", "run"])
    ap.add_argument("--patients", type=int, default=PATIENTS)
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--control", action="store_true", help="positive control: disengaged patients carry extra risk")
    args = ap.parse_args()
    if args.step == "build":
        build(args.patients, control=args.control)
    else:
        run(args.days)


if __name__ == "__main__":
    main()
