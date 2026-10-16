"""The outreach effect in the simulated world: pure rules, no database."""

from datetime import datetime

from readmit.sim.outcomes import Outcome, relative_reduction


def test_the_visit_helps_some_patients_more_than_others():
    assert relative_reduction("Heart failure", {"transport_barrier": True}) > relative_reduction("Pneumonia", {})
    assert relative_reduction("Pneumonia", {"dementia": True, "substance_use": True}) == 0.0
    assert relative_reduction("Heart failure", {"transport_barrier": True, "housing_unstable": True}) <= 0.6


def test_both_potential_outcomes_come_from_one_draw():
    o = Outcome("E1", "C1", p0=0.4, rr=0.5, u=0.3, readmit_at=datetime(2026, 9, 20))
    assert o.p1 == 0.2
    assert (o.u < o.p0, o.u < o.p1) == (True, False)  # readmitted with usual care, not after a visit
