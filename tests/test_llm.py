"""The LLM client's safety nets, tested with a fake model: no API calls, no cost."""
import dataclasses
import types

import pytest
from pydantic import BaseModel

from readmit import llm


class Answer(BaseModel):
    x: int


def fake_response(content):
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=3)
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Isolated cache, no waiting, no database logging, two fake models."""
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "_log", lambda *a, **k: None)
    test_settings = dataclasses.replace(llm.settings, llm_min_interval_s=0, llm_model="gemini/primary",
                                        llm_fallback_model="gemini/fallback")
    monkeypatch.setattr(llm, "settings", test_settings)
    calls = []

    def install(behavior):
        def completion(model, messages, **kwargs):
            calls.append(model)
            return behavior(model, messages)
        monkeypatch.setattr(llm.litellm, "completion", completion)
        return calls
    return install


def test_valid_json_is_returned_and_cached(fake):
    calls = fake(lambda m, msgs: fake_response('{"x": 5}'))
    first, meta1 = llm.complete_json("t", "v1", "sys", "q", Answer)
    second, meta2 = llm.complete_json("t", "v1", "sys", "q", Answer)
    assert first.x == second.x == 5
    assert (meta1.cached, meta2.cached) == (False, True)
    assert calls == ["gemini/primary"]                     # second answer came from the cache


def test_primary_down_falls_back(fake):
    def behavior(model, msgs):
        if model == "gemini/primary":
            raise RuntimeError("503 overloaded")
        return fake_response('{"x": 7}')
    calls = fake(behavior)
    result, meta = llm.complete_json("t", "v1", "sys", "q", Answer)
    assert result.x == 7 and meta.model == "gemini/fallback"
    assert calls == ["gemini/primary"] * llm.MAX_ATTEMPTS_PER_MODEL + ["gemini/fallback"]


def test_invalid_json_gets_one_repair(fake):
    fake(lambda m, msgs: fake_response("not json" if len(msgs) == 2 else '{"x": 1}'))
    result, meta = llm.complete_json("t", "v1", "sys", "q", Answer)
    assert result.x == 1 and meta.repaired


def test_all_models_failing_raises(fake):
    def behavior(model, msgs):
        raise RuntimeError("down")
    fake(behavior)
    with pytest.raises(llm.LLMError):
        llm.complete_json("t", "v1", "sys", "q", Answer)
