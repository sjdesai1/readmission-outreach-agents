"""Build the simulated hospital and load it into Postgres.

    python -m readmit.sim.generate              # 1,000 patients (default)
    python -m readmit.sim.generate --patients 200

Rebuilds everything from scratch each run (same seed = same world).
"""
import argparse

from readmit import db
from readmit.sim.world import World


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    tables = World(args.patients, args.seed).build()
    db.ensure_schemas()
    for name, df in tables.items():
        db.write(df, name)
        print(f"  wrote {name:<20} {len(df):>6} rows")

    enc = tables["sim.encounters"].drop_duplicates("encounter_id")
    pts = tables["sim.patients"]
    print(f"\n  patients {len(pts)} | stays {len(enc)} (history {int((~enc.outcome_pending).sum())}, "
          f"live {int(enc.outcome_pending.sum())}) | notes {len(tables['sim.notes'])}")


if __name__ == "__main__":
    main()
