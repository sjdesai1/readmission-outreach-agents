"""Reset the outreach system: tables, simulation clock, clinic calendar, one case per flagged discharge.

    python -m readmit.outreach.setup

Safe to re-run: it wipes outreach data and rebuilds it (same seed, same calendar).
Cases are opened for flagged, non-holdout live discharges. Each case only becomes "visible" to the
conversation loop once the simulated clock passes its opened_at (= prediction time, discharge + 24h).
"""

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from readmit import db
from readmit.outreach import store
from readmit.outreach.store import FAST_FOLLOW_UP
from readmit.sim import clock

PROVIDERS = [  # provider_id, name, role, modes offered in the morning / afternoon
    ("PRV01", "Dr. Ana Lopez", "primary care", ("in_person", "telehealth")),
    ("PRV02", "Dr. James Park", "primary care", ("in_person", "in_person")),
    ("PRV03", "Grace Kim, NP", "transitional care", ("telehealth", "telehealth")),
]
SPEAKS = {"PRV01": ["English", "Spanish"], "PRV02": ["English"], "PRV03": ["English"]}
PRIMARY_CARE = ["PRV01", "PRV02"]
TAKEN_BY_OTHERS = 0.70  # most slots already belong to other patients: availability is real
CALENDAR_END = clock.LIVE_END + timedelta(days=45)


def calendar(seed: int = 11, taken_by_others: float = TAKEN_BY_OTHERS) -> pd.DataFrame:
    """30-minute slots, weekdays 9:00-16:30, for each provider, across the live period. A bigger simulated
    program (sim/program.py) passes a lower taken_by_others, so the clinic can absorb 20x the patients."""
    rng = np.random.default_rng(seed)
    rows, day = [], clock.LIVE_START
    while day <= CALENDAR_END:
        if day.weekday() < 5:
            for pid, _, _, (am, pm) in PROVIDERS:
                for k in range(15):
                    start = day.replace(hour=9, minute=0) + timedelta(minutes=30 * k)
                    rows.append(
                        {
                            "slot_id": f"S{pid[-2:]}{start:%Y%m%d%H%M}",
                            "provider_id": pid,
                            "start_at": start,
                            "end_at": start + timedelta(minutes=30),
                            "mode": am if start.hour < 12 else pm,
                            "status": "held_other" if rng.random() < taken_by_others else "open",
                            "patient_id": None,
                            "appointment_id": None,
                        }
                    )
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def own_doctor(patient_id: str, pcp_assigned: bool) -> str | None:
    """Patients with a PCP get one of the two primary care doctors, the same one on every run."""
    if not pcp_assigned:
        return None
    return PRIMARY_CARE[int(hashlib.sha256(patient_id.encode()).hexdigest(), 16) % len(PRIMARY_CARE)]


def build_cases() -> pd.DataFrame:
    """One case per flagged, non-holdout live discharge. Joined columns get their own names, so nothing
    collides with a column scoring.scores already has (it has discharge_datetime too)."""
    scores = db.read("""select s.encounter_id, s.patient_id, s.prediction_time, s.tier, s.risk,
                               t.primary_dx_group as dx_group, t.discharge_datetime as discharged_at,
                               p.pcp_assigned
                        from scoring.scores s join cohort.stays t using (encounter_id)
                        join sim.patients p on p.patient_id = s.patient_id
                        where s.flagged and not s.holdout order by s.prediction_time""")
    discharged = pd.to_datetime(scores["discharged_at"])
    return pd.DataFrame(
        {
            "case_id": [f"C{e[1:]}" for e in scores["encounter_id"]],
            "encounter_id": scores["encounter_id"],
            "patient_id": scores["patient_id"],
            "opened_at": scores["prediction_time"],
            "tier": scores["tier"],
            "risk": scores["risk"],
            "status": "new",
            "next_action_at": scores["prediction_time"],
            "attempts": 0,
            "updated_at": scores["prediction_time"],
            "pcp_provider_id": [
                own_doctor(p, a) for p, a in zip(scores["patient_id"], scores["pcp_assigned"], strict=True)
            ],
            "follow_up_by": [
                d + timedelta(days=FAST_FOLLOW_UP.get(dx, 14))
                for d, dx in zip(discharged, scores["dx_group"], strict=True)
            ],
        }
    )  # closed_at / close_reason left out: all-empty columns get typed as text and Postgres rejects them


def wipe_outreach() -> list[str]:
    """Empty EVERY table in the outreach schema, found at run time, so a table added later can't be missed.
    (A hand-written list once left nurse tasks behind; a replayed idempotency key then returned a stale task.)"""
    with db.engine().begin() as c:
        tables = [
            r[0]
            for r in c.exec_driver_sql(
                "select table_name from information_schema.tables "
                "where table_schema = 'outreach' and table_type = 'BASE TABLE' order by table_name"
            )
        ]
        if tables:
            c.exec_driver_sql("truncate " + ", ".join(f"outreach.{t}" for t in tables))
    return tables


def main(taken_by_others: float = TAKEN_BY_OTHERS):
    db.ensure_schemas()
    with db.engine().begin() as c:  # migrations first: new columns must exist before anything is read
        c.exec_driver_sql((Path(__file__).parent / "schema.sql").read_text())
    # build everything BEFORE wiping: if building fails, the existing outreach data is left untouched
    cases = build_cases()
    slots = calendar(taken_by_others=taken_by_others)
    wipe_outreach()

    db.write(
        pd.DataFrame([{"provider_id": p, "name": n, "role": r} for p, n, r, _ in PROVIDERS]),
        "outreach.providers",
        if_exists="append",
    )
    for pid, langs in SPEAKS.items():
        db.execute("update outreach.providers set languages = :l where provider_id = :p", l=langs, p=pid)
    db.write(slots, "outreach.slots", if_exists="append")
    store.set_now(datetime.combine(clock.LIVE_START.date(), datetime.min.time()))
    db.write(cases, "outreach.cases", if_exists="append")

    print(
        f"  providers {len(PROVIDERS)} | slots {len(slots)} ({int((slots['status'] == 'open').sum())} open) "
        f"| cases {len(cases)} | clock {store.now()}"
    )
    first = cases.iloc[0]
    print(
        f"  first case: {first['case_id']} ({first['tier']}, opens {first['opened_at']}, "
        f"own doctor {first['pcp_provider_id']}, follow up by {first['follow_up_by']:%Y-%m-%d})"
    )


if __name__ == "__main__":
    main()
