"""Post-discharge signals and landmark features: pure rules, no database."""

import json

import numpy as np
import pandas as pd

from readmit.model.landmark import (
    UPDATE_FEATURES,
    apply_update,
    at_risk,
    fit_update,
    live_score,
    logit,
    nearest,
    odds_ratios,
    signal_features,
)
from readmit.sim.rescore import alert_priority, triggers_for
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
    assert f7.loc["E1"].to_dict() == {"rx_missing": 0, "rx_late": 0, "ed_visits": 0, "followup_kept": 0}
    assert f7.loc["E2", "rx_missing"] == 1 and f7.loc["E2", "followup_kept"] == 1
    assert signal_features(events, 14, ["E1"]).loc["E1", "ed_visits"] == 1


def test_only_patients_still_home_are_at_risk():
    stays = pd.DataFrame({"encounter_id": ["A", "B", "C"], "days_to_next_unplanned": [3.0, 20.0, None]})
    assert list(at_risk(stays, 7)["encounter_id"]) == ["B", "C"]  # A was back in the hospital on day 3


# ------------------------------------------------------------------ 7a-2: update the prior, re-score on events
def test_update_model_keeps_the_prior_and_learns_a_real_signal():
    rng = np.random.default_rng(0)
    n = 4000
    prior = rng.uniform(0.05, 0.6, n)
    ed = rng.binomial(1, 0.1, n)
    z = logit(prior) + 1.2 * ed  # truth: an ED visit multiplies the odds by e^1.2 (about 3.3)
    y = rng.binomial(1, 1 / (1 + np.exp(-z)))
    X = pd.DataFrame({"prior_logit": logit(prior), "rx_missing": 0, "rx_late": 0, "ed_visits": ed, "followup_kept": 0})
    model = fit_update(X, y)
    assert 2.0 < odds_ratios(model)["ed_visits"] < 5.5
    p = apply_update(model, X)
    assert p[(ed == 1)].mean() > p[(ed == 0)].mean()
    assert np.corrcoef(p[ed == 0], prior[ed == 0])[0, 1] > 0.95  # no news: close to the discharge risk
    json.dumps(model)  # saved as plain JSON


def test_live_score_sees_only_events_before_the_day():
    model = {
        "features": UPDATE_FEATURES,
        "mean": dict.fromkeys(UPDATE_FEATURES, 0.0),
        "scale": dict.fromkeys(UPDATE_FEATURES, 1.0),
        "coef": {"prior_logit": 1.0, "rx_missing": 0.0, "rx_late": 0.0, "ed_visits": 1.0, "followup_kept": 0.0},
        "intercept": 0.0,
    }
    events = pd.DataFrame([{"encounter_id": "E1", "day": 5.0, "type": "ed_visit"}])
    assert live_score(model, "E1", 0.2, events, 4.0) == 0.2
    assert live_score(model, "E1", 0.2, events, 5.1) > 0.35


def test_event_rescores_use_the_nearest_landmark_model():
    models = {7: "d7", 14: "d14", 21: "d21"}
    assert nearest(models, 3.0) == "d7" and nearest(models, 12.0) == "d14" and nearest(models, 25.0) == "d21"


def test_triggers_ed_visits_and_a_missed_fill():
    events = pd.DataFrame(
        [
            {"encounter_id": "E1", "day": 6.0, "type": "rx_fill"},
            {"encounter_id": "E1", "day": 9.0, "type": "ed_visit"},
        ]
    )
    (d1, t1), (d2, t2) = triggers_for(events)
    assert (d1, t1) == (3.0, "missed_fill") and t2 == "ed_visit" and 9.0 < d2 < 9.1  # seen an hour later
    on_time = pd.DataFrame([{"encounter_id": "E2", "day": 1.0, "type": "rx_fill"}])
    assert triggers_for(on_time) == []


def test_nurse_alert_only_when_an_event_raises_the_risk():
    assert alert_priority("ed_visit", 0.30, 0.42) == "urgent"
    assert alert_priority("missed_fill", 0.30, 0.36) == "routine"
    assert alert_priority("missed_fill", 0.30, 0.32) is None  # small change: outreach just reorders
    assert alert_priority("missed_fill", 0.55, 0.56) == "routine"  # already high
    assert alert_priority("landmark", 0.30, 0.60) is None


def test_a_missed_fill_reads_the_same_on_any_day():
    events = pd.DataFrame([{"encounter_id": "E1", "day": 6.0, "type": "rx_fill"}])
    assert signal_features(events, 3, ["E1", "E2"])["rx_missing"].tolist() == [1, 1]
    f14 = signal_features(events, 14, ["E1", "E2"])
    assert f14["rx_missing"].tolist() == [0, 1] and f14["rx_late"].tolist() == [1, 0]
