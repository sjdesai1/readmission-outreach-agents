"""Per-turn evals: the dataset is well formed, scoring is right, and the rule-only cases pass."""

import asyncio

import pytest

from readmit.config import REPO_ROOT
from readmit.evals.turns import load_cases, run_case, score, summarize

KNOWN = {
    "intent", "route", "route_has", "route_lacks", "action", "status", "nurse", "booked", "booked_count_max",
    "referral", "remembered", "reply_has_any", "reply_lacks", "no_reply", "status_not",
}  # fmt: skip


def facts(**kw):
    base = {
        "intent": None, "path": [], "action": None, "status": "new", "nurse_task": None, "booked_count": 0,
        "referrals": [], "memory": [], "sent": [], "reason": None,
    }  # fmt: skip
    return {**base, **kw}


def test_dataset_is_well_formed():
    cases = load_cases()
    assert len(cases) >= 35
    for c in cases:
        assert c["case"] in ("C1", "C2") and c.get("setup", "fresh") in ("fresh", "offered", "booked", "paused")
        assert set(c["expect"]) <= KNOWN, c["id"]
        assert c.get("trigger", "inbound_reply") != "inbound_reply" or (c.get("reply") and c.get("channel"))


def test_scoring_passes_and_fails_the_right_checks():
    f = facts(intent="symptom", path=["load_context", "escalate"], sent=["Call 911 if it gets worse"],
              nurse_task={"kind": "symptom", "priority": "urgent"})  # fmt: skip
    rows = score({"intent": ["symptom"], "nurse": {"priority": "urgent"}, "reply_has_any": ["911"]}, f, "C1")
    assert all(r["ok"] for r in rows) and rows[-1]["check"] == "privacy"
    bad = score({"nurse": False, "reply_lacks": ["911"]}, f, "C1")
    assert [r["ok"] for r in bad] == [False, False, True]


def test_privacy_check_catches_the_other_patients_details():
    rows = score({}, facts(sent=["Hi Maria, Jose also has a visit"]), "C1")
    assert rows == [{"check": "privacy", "ok": False, "expected": "no other patient's details", "got": ["Jose"]}]


def test_unknown_check_is_an_error():
    with pytest.raises(ValueError):
        score({"vibes": True}, facts(), "C1")


def test_summary_counts_cases_and_checks():
    results = [
        {"category": "a", "passed": True, "checks": [{"check": "x", "ok": True}]},
        {"category": "a", "passed": False, "checks": [{"check": "x", "ok": False}]},
    ]
    s = summarize(results)
    assert s["overall"]["passed"] == 1 and s["by_category"]["a"]["total"] == 2 and s["by_check"]["x"]["rate"] == 0.5


@pytest.mark.usefixtures("pg")
@pytest.mark.parametrize("case_id", ["stop-keyword", "emergency-chest-pain", "first-outreach", "paused-hello"])
def test_rule_routed_cases_pass_with_the_rule_based_agents(case_id):
    case = next(c for c in load_cases() if c["id"] == case_id)
    r = asyncio.run(run_case(case, None))
    assert r["passed"], [c for c in r["checks"] if not c["ok"]]


DEFENSES = {
    "input_guard", "identity_check", "reply_interpreter", "pseudonymization", "code_owned_facts", "mcp_permissions",
    "output_checks", "reviewer", "nurse_handoff",
}  # fmt: skip


def test_red_team_dataset_is_well_formed():
    cases = load_cases(REPO_ROOT / "evals" / "redteam.jsonl")
    assert len(cases) >= 30
    for c in cases:
        assert c["defense"] in DEFENSES, c["id"]
        assert set(c["expect"]) <= KNOWN, c["id"]
        assert c["case"] in ("C1", "C2") and c.get("reply") and c.get("channel")


def test_status_not_check():
    rows = score({"status_not": "opted_out"}, facts(status="opted_out"), "C2")
    assert rows[0]["ok"] is False
