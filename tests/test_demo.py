"""make demo (milestone 7): the build steps, the readiness check, and the closing summary."""

import asyncio
from datetime import timedelta

import pytest

from readmit import demo
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn


def test_without_a_key_the_social_factors_come_from_keyword_rules():
    assert ["readmit.extract.social", "--rules"] in demo.build_steps(has_key=False)
    assert ["readmit.extract.social"] in demo.build_steps(has_key=True)


def test_the_pipeline_runs_in_order():
    order = [s[0] for s in demo.build_steps(has_key=True)]
    assert order.index("readmit.sim.generate") < order.index("readmit.data.cohort") < order.index("readmit.model.train")
    assert (
        order.index("readmit.model.train") < order.index("readmit.model.score") < order.index("readmit.model.landmark")
    )


def test_the_report_names_cases_and_links():
    s = {"cases": 3, "by_status": {"booked": 2, "closed": 1}, "sent": 9, "replies": 4, "booked": 2, "attended": 1,
         "nurse_tasks": 1, "urgent": 0, "readmitted": 0}  # fmt: skip
    text = demo.report(s, [{"label": "Readmitted", "case_id": "C7", "messages": 4}])
    assert "3 outreach cases: booked 2, closed 1" in text and "C7" in text and "http://localhost:8502" in text


@pytest.mark.usefixtures("pg")
def test_built_and_summary_read_the_database():
    assert demo.built()  # the fixture has scores
    asyncio.run(run_turn(Gateway.in_memory(), "C1", "outreach_due"))
    s = demo.summary()
    assert s["cases"] == 2 and s["sent"] == 1 and s["replies"] == 0 and s["readmitted"] == 0
    assert "not discharged yet" not in s["by_status"]  # both fixture cases opened yesterday


def test_the_default_run_lets_every_case_finish():
    from readmit.sim import clock

    end = clock.LIVE_START + timedelta(days=demo.DAYS)
    assert end >= clock.LIVE_END + timedelta(days=30)  # the last discharge still gets its 30-day outcome
