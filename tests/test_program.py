"""Step 7b: the bigger program's simulated extraction and the outreach signal features (pure, no database)."""

from datetime import datetime

import numpy as np
import pandas as pd

from readmit.model.outreach_signals import OUTREACH, kept_visits, outreach_features
from readmit.sim.notes import SOCIAL_FACTORS
from readmit.sim.program import simulated_extraction

D = datetime(2026, 9, 1, 12, 0)


def test_simulated_extraction_matches_the_measured_error_rates():
    n = 20000
    rng = np.random.default_rng(0)
    documented = pd.DataFrame({"encounter_id": [f"E{i}" for i in range(n)]})
    for f in SOCIAL_FACTORS:
        documented[f] = rng.random(n) < 0.2
    rates = {f: {"recall": 0.8, "false_positive_rate": 0.02} for f in SOCIAL_FACTORS}
    out = simulated_extraction(documented, rates, seed=1)
    f = SOCIAL_FACTORS[0]
    truth, pred = documented[f], out[f]
    assert abs(pred[truth].mean() - 0.8) < 0.03
    assert abs(pred[~truth].mean() - 0.02) < 0.01
    assert out["extracted"].all() and (out["prompt_version"] == "simulated").all()


def test_outreach_features_only_see_what_happened_before_the_landmark():
    cases = pd.DataFrame({"case_id": ["A", "B", "C"], "discharge": [D, D, D]})
    day = pd.Timedelta(days=1)
    msgs = pd.DataFrame(
        [
            {"case_id": "A", "direction": "outbound", "author": "ai", "status": "sent", "created_at": D + day},
            {
                "case_id": "A",
                "direction": "inbound",
                "author": "patient",
                "status": "received",
                "created_at": D + 2 * day,
            },
            {"case_id": "B", "direction": "outbound", "author": "ai", "status": "sent", "created_at": D + day},
            {"case_id": "B", "direction": "outbound", "author": "ai", "status": "failed", "created_at": D + 3 * day},
            {
                "case_id": "C",
                "direction": "inbound",
                "author": "patient",
                "status": "received",
                "created_at": D + 9 * day,
            },
        ]
    )
    appts = pd.DataFrame(
        [
            {"case_id": "A", "status": "no_show", "start_at": D + 5 * day, "created_at": D + 2 * day},
            {"case_id": "C", "status": "completed", "start_at": D + 10 * day, "created_at": D + 9 * day},
        ]
    )
    barriers = pd.DataFrame([{"case_id": "A", "at": D + 2 * day}])
    f = outreach_features(cases, msgs, appts, barriers, 7)
    assert list(f.columns) == OUTREACH
    assert f.loc["A"].to_dict() == {
        "replied": 1,
        "silent": 0,
        "booked": 1,
        "no_show": 1,
        "barrier": 1,
        "unreachable": 0,
    }
    assert f.loc["B"].to_dict() == {
        "replied": 0,
        "silent": 1,
        "booked": 0,
        "no_show": 0,
        "barrier": 0,
        "unreachable": 1,
    }
    assert f.loc["C"].sum() == 0  # everything for C happened after day 7
    assert outreach_features(cases, msgs, appts, barriers, 14).loc["C", "replied"] == 1


def test_completed_visits_become_kept_follow_ups():
    cases = pd.DataFrame({"case_id": ["A"], "encounter_id": ["E1"], "discharge": [D]})
    appts = pd.DataFrame(
        [
            {"case_id": "A", "status": "completed", "start_at": D + pd.Timedelta(days=4), "created_at": D},
            {"case_id": "A", "status": "cancelled", "start_at": D + pd.Timedelta(days=2), "created_at": D},
        ]
    )
    k = kept_visits(cases, appts)
    assert k.to_dict("records") == [{"encounter_id": "E1", "day": 4.0, "type": "followup_kept"}]
