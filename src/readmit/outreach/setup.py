"""Reset the outreach system: tables, simulation clock, clinic calendar, one case per flagged discharge.

    python -m readmit.outreach.setup

Safe to re-run: it wipes outreach data and rebuilds it (same seed, same calendar).
Cases are opened for flagged, non-holdout live discharges. Each case only becomes "visible" to the
conversation loop once the simulated clock passes its opened_at (= prediction time, discharge + 24h).
"""
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from readmit import db
from readmit.outreach import store
from readmit.sim import clock

PROVIDERS = [  # provider_id, name, role, modes offered in the morning / afternoon
    ("PRV01", "Dr. Ana Lopez", "primary care", ("in_person", "telehealth")),
    ("PRV02", "Dr. James Park", "primary care", ("in_person", "in_person")),
    ("PRV03", "Grace Kim, NP", "transitional care", ("telehealth", "telehealth")),
]
TAKEN_BY_OTHERS = 0.70        # most slots already belong to other patients: availability is real
CALENDAR_END = clock.LIVE_END + timedelta(days=45)


def calendar(seed: int = 11) -> pd.DataFrame:
    """30-minute slots, weekdays 9:00-16:30, for each provider, across the live period."""
    rng = np.random.default_rng(seed)
    rows, day = [], clock.LIVE_START
    while day <= CALENDAR_END:
        if day.weekday() < 5:
            for pid, _, _, (am, pm) in PROVIDERS:
                for k in range(15):
                    start = day.replace(hour=9, minute=0) + timedelta(minutes=30 * k)
                    rows.append({"slot_id": f"S{pid[-2:]}{start:%Y%m%d%H%M}", "provider_id": pid, "start_at": start,
                                 "end_at": start + timedelta(minutes=30), "mode": am if start.hour < 12 else pm,
                                 "status": "held_other" if rng.random() < TAKEN_BY_OTHERS else "open",
                                 "patient_id": None, "appointment_id": None})
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def main():
    db.ensure_schemas()
    with db.engine().begin() as c:
        c.exec_driver_sql((Path(__file__).parent / "schema.sql").read_text())
        c.exec_driver_sql("truncate outreach.events, outreach.messages, outreach.appointments, outreach.referrals, "
                          "outreach.cases, outreach.slots, outreach.providers, outreach.sim_clock")

    db.write(pd.DataFrame([{"provider_id": p, "name": n, "role": r} for p, n, r, _ in PROVIDERS]),
             "outreach.providers", if_exists="append")
    slots = calendar()
    db.write(slots, "outreach.slots", if_exists="append")
    store.set_now(datetime.combine(clock.LIVE_START.date(), datetime.min.time()))

    scores = db.read("select * from scoring.scores where flagged and not holdout order by prediction_time")
    cases = pd.DataFrame({
        "case_id": [f"C{e[1:]}" for e in scores["encounter_id"]],
        "encounter_id": scores["encounter_id"], "patient_id": scores["patient_id"],
        "opened_at": scores["prediction_time"], "tier": scores["tier"], "risk": scores["risk"],
        "status": "new", "next_action_at": scores["prediction_time"], "attempts": 0,
        "updated_at": scores["prediction_time"],
    })   # closed_at / close_reason left out: all-empty columns get typed as text and Postgres rejects them
    db.write(cases, "outreach.cases", if_exists="append")

    print(f"  providers {len(PROVIDERS)} | slots {len(slots)} ({int((slots['status'] == 'open').sum())} open) "
          f"| cases {len(cases)} | clock {store.now()}")
    print(f"  first case: {cases.iloc[0]['case_id']} ({cases.iloc[0]['tier']}, opens {cases.iloc[0]['opened_at']})")


if __name__ == "__main__":
    main()
