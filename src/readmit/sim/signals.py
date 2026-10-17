"""Post-discharge signals: what a hospital learns in the weeks after discharge, outreach or not.

    python -m readmit.sim.signals        # history (train/test stays); live stays are added by the simulation

  rx_fill        pharmacy claim: discharge prescriptions filled (on time, late, or never)
  ed_visit       ED or urgent care visit, sent home: often a warning sign a few days before a readmission
  followup_kept  a follow-up visit that happened (history only; live visits come from the outreach system)

Generated from the hidden truth, so the signals carry real information: patients who don't take their
medicines often don't fill them, and patients heading for a readmission often show up in the ED first.
Nothing happens after a readmission (the patient is in the hospital). Seeded per stay: reruns match.
"""

import pandas as pd

from readmit import db
from readmit.sim.patients import seeded

WINDOW = 30
COLUMNS = ["encounter_id", "day", "type"]


def events_for(
    encounter_id: str,
    days_to_readmit: float | None,
    nonadherent: bool,
    transport_barrier: bool,
    followup_scheduled: bool,
    seed: int = 7,
) -> list[dict]:
    """Signal events for one stay, in days after discharge, all before any readmission."""
    r = seeded(seed, "signals", encounter_id)
    soon = days_to_readmit is not None and days_to_readmit <= 14
    end = min(days_to_readmit if days_to_readmit is not None else WINDOW, WINDOW)
    out = []

    p_on_time = 0.85 - 0.40 * nonadherent - 0.10 * soon
    if r.random() < p_on_time:
        out.append({"day": r.uniform(0.2, 3.0), "type": "rx_fill"})
    elif r.random() < 0.35:
        out.append({"day": r.uniform(5.0, 14.0), "type": "rx_fill"})  # filled late

    if days_to_readmit is not None and days_to_readmit <= WINDOW and r.random() < 0.40:
        out.append({"day": max(0.5, days_to_readmit - r.uniform(1.0, 6.0)), "type": "ed_visit"})
    if r.random() < 0.06:
        out.append({"day": r.uniform(1.0, 28.0), "type": "ed_visit"})  # unrelated visit: noise

    if followup_scheduled and r.random() < (0.50 if transport_barrier else 0.78):
        out.append({"day": r.uniform(3.0, 7.0), "type": "followup_kept"})

    return [{"encounter_id": encounter_id, **e} for e in out if e["day"] < end]


def build_history(seed: int = 7) -> pd.DataFrame:
    """Signals for every stay with a known outcome (train, test, gap)."""
    stays = db.read(
        """select s.encounter_id, s.days_to_next_unplanned, s.followup_scheduled_7d,
                  t.med_nonadherence, t.transport_barrier
           from cohort.stays s join truth.patients t using (patient_id)
           where s.split in ('train', 'test', 'gap')"""
    )
    rows = []
    for s in stays.itertuples():
        gap = None if pd.isna(s.days_to_next_unplanned) else float(s.days_to_next_unplanned)
        rows += events_for(
            s.encounter_id,
            gap,
            bool(s.med_nonadherence),
            bool(s.transport_barrier),
            bool(s.followup_scheduled_7d),
            seed,
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def build_live(outcomes: dict, seed: int = 7) -> pd.DataFrame:
    """Signals for live flagged stays, from the simulation's outcome draws. The ED warning sign follows the
    usual-care outcome (u < p0): a visit can still prevent the readmission after the warning sign appears.
    Follow-up visits are left out: for live stays they come from the outreach system's appointments."""
    with db.transaction() as c:
        if not db.one(c, "select to_regclass('sim.encounters') is not null as ok")["ok"]:
            return pd.DataFrame(columns=COLUMNS)
        hidden = {
            r["encounter_id"]: r
            for r in db.rows(
                c,
                """select e.encounter_id, t.med_nonadherence, t.transport_barrier
                   from (select distinct on (encounter_id) encounter_id, patient_id from sim.encounters) e
                   join truth.patients t using (patient_id)""",
            )
        }
    rows = []
    for o in outcomes.values():
        h = hidden.get(o.encounter_id)
        if not h or o.discharge is None:
            continue
        gap = (o.readmit_at - o.discharge).total_seconds() / 86400 if o.u < o.p0 else None
        rows += events_for(o.encounter_id, gap, bool(h["med_nonadherence"]), bool(h["transport_barrier"]), False, seed)
    return pd.DataFrame(rows, columns=COLUMNS)


def save(events: pd.DataFrame, replace_live: bool = False) -> None:
    """History replaces the table; live events replace only the live stays' rows."""
    db.execute("create schema if not exists sim")
    if not replace_live:
        db.write(events, "sim.post_discharge")
        return
    db.execute("create table if not exists sim.post_discharge (encounter_id text, day double precision, type text)")
    db.execute(
        """delete from sim.post_discharge where encounter_id in
           (select encounter_id from scoring.scores where flagged)"""
    )
    db.write(events, "sim.post_discharge", if_exists="append")


def main() -> None:
    events = build_history()
    save(events)
    counts = events["type"].value_counts().to_dict() if len(events) else {}
    print(f"  wrote sim.post_discharge: {len(events)} events {counts}")


if __name__ == "__main__":
    main()
