"""Label rules are where readmission projects quietly go wrong. Each rule gets a test."""

import pandas as pd

from readmit.data.cohort import add_labels, build


def stay(eid, pid, admit, discharge, planned=False, dispo="Home", pending=False):
    return {
        "encounter_id": eid,
        "patient_id": pid,
        "admit_datetime": pd.Timestamp(admit),
        "discharge_datetime": pd.Timestamp(discharge),
        "planned_admission": planned,
        "discharge_disposition": dispo,
        "outcome_pending": pending,
    }


def labels(rows):
    out = add_labels(pd.DataFrame(rows))
    return dict(zip(out["encounter_id"], out["readmit_30d"]))


def test_unplanned_return_within_30_days_is_a_readmission():
    assert labels([stay("A", "P1", "2025-01-01", "2025-01-05"), stay("B", "P1", "2025-01-20", "2025-01-25")])["A"] == 1


def test_return_after_30_days_is_not():
    assert labels([stay("A", "P1", "2025-01-01", "2025-01-05"), stay("B", "P1", "2025-02-20", "2025-02-25")])["A"] == 0


def test_planned_admission_does_not_count():
    assert (
        labels(
            [stay("A", "P1", "2025-01-01", "2025-01-05"), stay("B", "P1", "2025-01-15", "2025-01-17", planned=True)]
        )["A"]
        == 0
    )


def test_unplanned_after_a_planned_one_still_counts_if_within_30_days():
    assert (
        labels(
            [
                stay("A", "P1", "2025-01-01", "2025-01-05"),
                stay("B", "P1", "2025-01-10", "2025-01-12", planned=True),
                stay("C", "P1", "2025-01-25", "2025-01-28"),
            ]
        )["A"]
        == 1
    )


def test_other_patients_never_count():
    assert labels([stay("A", "P1", "2025-01-01", "2025-01-05"), stay("B", "P2", "2025-01-10", "2025-01-12")])["A"] == 0


def test_label_before_exclusion_and_death_stay_excluded():
    """The second stay ends in death: it's excluded, but it still makes the first stay a readmission."""
    rows = [stay("A", "P1", "2025-01-01", "2025-01-05"), stay("B", "P1", "2025-01-12", "2025-01-15", dispo="Expired")]
    notes = pd.DataFrame(columns=["note_id", "encounter_id", "note_type", "note_datetime"])
    facts = pd.DataFrame(columns=["note_id", "encounter_id", "factor"])
    stays, _, _, report = build(pd.DataFrame(rows), notes.astype({"note_datetime": "datetime64[ns]"}), facts)
    assert stays.set_index("encounter_id").loc["A", "readmit_30d"] == 1
    assert "B" not in set(stays["encounter_id"])
    assert report["excluded_death_or_hospice"] == 1


def test_notes_after_prediction_time_are_dropped():
    rows = [stay("A", "P1", "2025-01-01", "2025-01-05 12:00")]
    notes = pd.DataFrame(
        [
            {
                "note_id": "n1",
                "encounter_id": "A",
                "note_type": "H&P",
                "note_datetime": pd.Timestamp("2025-01-01 08:00"),
            },
            {
                "note_id": "n2",
                "encounter_id": "A",
                "note_type": "Post-discharge call",
                "note_datetime": pd.Timestamp("2025-01-08 09:00"),
            },
        ]
    )
    facts = pd.DataFrame([{"note_id": "n2", "encounter_id": "A", "factor": "lives_alone"}])
    _, kept, documented, _ = build(pd.DataFrame(rows), notes, facts)
    assert list(kept["note_id"]) == ["n1"]
    assert not documented.set_index("encounter_id").loc["A", "lives_alone"]  # only mentioned in a dropped note
