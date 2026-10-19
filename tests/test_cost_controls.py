"""Step 8 cost controls: the daily LLM spend cap and one outreach message per patient per day."""

import asyncio
from datetime import datetime

import pytest
from pydantic import BaseModel

from readmit import llm
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.agents.stubs import STUBS
from readmit.outreach import store


class Answer(BaseModel):
    ok: bool


def test_over_budget_refuses_new_calls_without_touching_the_api(monkeypatch):
    monkeypatch.setattr(llm, "budget_exhausted", lambda: True)
    monkeypatch.setattr(llm, "_log", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_call_with_retries", lambda *a, **k: pytest.fail("API called over budget"))
    with pytest.raises(llm.LLMBudgetExceeded):
        llm.complete_json("t", "v1", "system", "user", Answer, use_cache=False)


def test_over_budget_still_serves_cached_answers(monkeypatch):
    monkeypatch.setattr(llm, "budget_exhausted", lambda: True)
    monkeypatch.setattr(llm, "_log", lambda *a, **k: None)
    hit = {"content": '{"ok": true}', "usage_metadata": {"input_tokens": 5, "output_tokens": 2}, "latency_ms": 0}
    monkeypatch.setattr(llm, "_cache_get", lambda key: hit)
    parsed, meta = llm.complete_json("t", "v1", "system", "user", Answer)
    assert parsed.ok and meta.cached


def test_budget_exceeded_is_an_llm_error():
    assert issubclass(llm.LLMBudgetExceeded, llm.LLMError)  # every agent's fallback path also covers it


@pytest.mark.usefixtures("pg")
def test_over_budget_turns_run_on_the_rule_based_agents(monkeypatch):
    monkeypatch.setattr(llm, "budget_exhausted", lambda: True)

    async def boom(state, gw):
        pytest.fail("an LLM agent ran over budget")

    s = asyncio.run(run_turn(Gateway.in_memory(), "C1", "outreach_due", agents=dict.fromkeys(STUBS, boom)))
    assert (s.get("outcome") or {}).get("action") == "sent"


@pytest.mark.usefixtures("pg")
def test_one_outreach_message_per_patient_per_day():
    assert "C1" in [c["case_id"] for c in store.due_cases()]
    store.send_message("C1", "email", "Checking in", "today-1")
    assert "C1" not in [c["case_id"] for c in store.due_cases()]  # already messaged today
    store.set_now(datetime(2026, 9, 10, 9, 0))
    assert "C1" in [c["case_id"] for c in store.due_cases()]  # a new day
