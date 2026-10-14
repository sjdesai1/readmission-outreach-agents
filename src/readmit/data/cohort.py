"""Build the cohort: labels, exclusions, time split, and the notes available at prediction time.

    python -m readmit.data.cohort

Reads sim.* and truth.note_facts. Writes:
  cohort.stays        one row per eligible stay: features, label, split, new vs repeat patient
  cohort.notes        notes written by prediction time (discharge + 24h)
  truth.documented    per stay, which social factors those notes actually mention (extraction answer key)
"""

import json

import pandas as pd

from readmit.sim import clock
from readmit.sim.notes import SOCIAL_FACTORS

EXCLUDED_DISPOSITIONS = {"Expired", "Hospice"}


def add_labels(enc: pd.DataFrame) -> pd.DataFrame:
    """Days to the same patient's next UNPLANNED admission, and the 30-day label.

    Done before any exclusion: a stay that ended in death can still be someone's readmission.
    """
    enc = enc.sort_values(["patient_id", "admit_datetime"]).copy()
    days = []
    for _, g in enc.groupby("patient_id", sort=False):
        admits, planned = g["admit_datetime"].tolist(), g["planned_admission"].tolist()
        for i, dis in enumerate(g["discharge_datetime"]):
            gap = None
            for j in range(i + 1, len(admits)):
                if not planned[j] and admits[j] > dis:
                    gap = (admits[j] - dis).total_seconds() / 86400
                    break
            days.append(gap)
    enc["days_to_next_unplanned"] = pd.to_numeric(pd.Series(days, index=enc.index), errors="coerce")
    enc["readmit_30d"] = (enc["days_to_next_unplanned"] <= clock.READMIT_WINDOW_DAYS).astype("Int64")
    return enc


def assign_split(enc: pd.DataFrame) -> pd.Series:
    d = enc["discharge_datetime"]
    split = pd.Series("gap", index=enc.index)  # labels not mature by go-live: unused
    split[d < clock.TRAIN_END] = "train"
    split[(d >= clock.TRAIN_END) & (d < clock.TEST_END)] = "test"
    split[enc["outcome_pending"]] = "live"
    return split


def build(enc: pd.DataFrame, notes: pd.DataFrame, note_facts: pd.DataFrame):
    report = {"raw_rows": len(enc)}
    enc = enc.drop_duplicates("encounter_id")
    report["duplicates_dropped"] = report["raw_rows"] - len(enc)

    enc = add_labels(enc)
    excluded = enc["discharge_disposition"].isin(EXCLUDED_DISPOSITIONS)
    report["excluded_death_or_hospice"] = int(excluded.sum())
    enc = enc[~excluded].copy()

    enc["split"] = assign_split(enc)
    enc.loc[enc["split"] == "live", ["readmit_30d", "days_to_next_unplanned"]] = pd.NA  # not decided yet
    train_patients = set(enc.loc[enc["split"] == "train", "patient_id"])
    enc["patient_status"] = enc["patient_id"].map(lambda p: "repeat" if p in train_patients else "new")
    enc.loc[enc["split"] == "train", "patient_status"] = "train"
    enc["prediction_time"] = enc["discharge_datetime"] + pd.Timedelta(hours=clock.PREDICTION_LAG_HOURS)

    # notes: only what exists at prediction time
    cutoff = enc.set_index("encounter_id")["prediction_time"]
    notes = notes[notes["encounter_id"].isin(cutoff.index)].copy()
    late = notes["note_datetime"] > notes["encounter_id"].map(cutoff)
    report["notes_dropped_after_prediction_time"] = notes.loc[late, "note_type"].value_counts().to_dict()
    kept = notes[~late].copy()
    report["notes_kept"] = len(kept)

    # answer key: social factors actually written in the kept notes
    facts = note_facts[note_facts["note_id"].isin(set(kept["note_id"]))]
    documented = (pd.crosstab(facts["encounter_id"], facts["factor"]) > 0).reindex(
        columns=SOCIAL_FACTORS, fill_value=False
    )
    documented = documented.reindex(enc["encounter_id"], fill_value=False).reset_index()
    documented.columns = ["encounter_id"] + SOCIAL_FACTORS

    for s in ["train", "test", "gap", "live"]:
        part = enc[enc["split"] == s]
        report[s] = {
            "stays": len(part),
            "patients": int(part["patient_id"].nunique()),
            "readmit_rate": None if s == "live" else round(float(part["readmit_30d"].astype(float).mean()), 3),
            "discharges": [str(part["discharge_datetime"].min().date()), str(part["discharge_datetime"].max().date())]
            if len(part)
            else None,
        }
    test = enc[enc["split"] == "test"]
    report["test_readmit_rate_by_patient_status"] = (
        test.groupby("patient_status")["readmit_30d"].apply(lambda s: round(float(s.astype(float).mean()), 3)).to_dict()
    )
    report["documented_factor_rate"] = documented[SOCIAL_FACTORS].mean().round(3).to_dict()
    return enc, kept, documented, report


def main():
    from readmit import db

    enc = db.read("select * from sim.encounters")
    notes = db.read("select * from sim.notes")
    facts = db.read("select * from truth.note_facts")
    stays, kept, documented, report = build(enc, notes, facts)
    db.write(stays, "cohort.stays")
    db.write(kept, "cohort.notes")
    db.write(documented, "truth.documented")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
