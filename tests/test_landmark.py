"""Post-discharge signals and landmark features: pure rules, no database."""

import pandas as pd

from readmit.model.landmark import at_risk, signal_features
from readmit.sim.signals import events_for


def test_nothing_happens_after_the_readmission():
    for i in range(200):
        ev = events_for(
            f"E{i}", days_to_readmit=6.0, nonadherent=False, transport_barrier=False, followup_scheduled=True
        )
        assert all(e["day"] < 6.0 for e in ev)


def test_signals_are_seeded_per_stay():
    a = events_for("E1", 12.0, True, False, True)
    assert a == events_for("E1", 12.0, True, False, True)


def test_signals_carry_real_information():
    n = 2000

    def ed(gap):
        return sum(
            any(e["type"] == "ed_visit" for e in events_for(f"E{i}", gap, False, False, False)) for i in range(n)
        )

    def fills(nonadherent):
        return sum(
            any(e["type"] == "rx_fill" and e["day"] <= 3 for e in events_for(f"F{i}", None, nonadherent, False, False))
            for i in range(n)
        )

    assert ed(20.0) > 4 * ed(None)  # an ED visit is a warning sign before a readmission
    assert fills(True) < 0.7 * fills(False)  # patients who don't take their medicines often don't fill them


def test_landmark_features_only_see_the_past():
    events = pd.DataFrame(
        [
            {"encounter_id": "E1", "day": 2.0, "type": "rx_fill"},
            {"encounter_id": "E1", "day": 9.0, "type": "ed_visit"},
            {"encounter_id": "E2", "day": 4.0, "type": "followup_kept"},
        ]
    )
    f7 = signal_features(events, 7, ["E1", "E2"])
    assert f7.loc["E1"].to_dict() == {"rx_filled": 1, "rx_days_to_fill": 2.0, "ed_visits": 0, "followup_kept": 0}
    assert f7.loc["E2", "rx_days_to_fill"] == 7.0 and f7.loc["E2", "followup_kept"] == 1
    assert signal_features(events, 14, ["E1"]).loc["E1", "ed_visits"] == 1


def test_only_patients_still_home_are_at_risk():
    stays = pd.DataFrame({"encounter_id": ["A", "B", "C"], "days_to_next_unplanned": [3.0, 20.0, None]})
    assert list(at_risk(stays, 7)["encounter_id"]) == ["B", "C"]  # A was back in the hospital on day 3
