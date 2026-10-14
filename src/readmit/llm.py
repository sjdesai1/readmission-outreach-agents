"""The one LLM client every part of the project uses.

    result = complete_json(task="extract_social", prompt_version="v1", system=..., user=...,
                           schema=MyPydanticModel)

What it handles, so no caller has to:
  * fallback       primary model fails after retries -> fallback model
  * retries        exponential backoff on errors (2s, 4s, 8s)
  * rate limiting  minimum spacing between real calls (free tier limits)
  * caching        identical request -> stored answer, no API call (reruns are free)
  * validation     output must parse as JSON and match the Pydantic schema; one repair retry
  * tracing        every real call is a LangSmith run with task, prompt version, model, tokens
  * cost log       every call (cached or not) is a row in ops.llm_calls: tokens, cost, latency, outcome
"""

import hashlib
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import litellm
from langsmith import traceable
from pydantic import BaseModel, ValidationError

from readmit.config import REPO_ROOT, settings

CACHE_DIR = REPO_ROOT / "data" / "llm_cache"
MAX_ATTEMPTS_PER_MODEL = 3
litellm.suppress_debug_info = True


class LLMError(RuntimeError):
    pass


@dataclass
class CallMeta:
    model: str
    cached: bool
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    repaired: bool = False


# ------------------------------------------------------------------ rate limiting
_lock = threading.Lock()
_last_call = 0.0


def _wait_turn():
    """Space real API calls at least LLM_MIN_INTERVAL_S apart (thread-safe)."""
    global _last_call
    with _lock:
        gap = settings.llm_min_interval_s - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.monotonic()


# ------------------------------------------------------------------ cache
def _cache_key(model, messages):
    return hashlib.sha256(json.dumps([model, messages], sort_keys=True).encode()).hexdigest()


def _cache_get(key) -> dict | None:
    f = CACHE_DIR / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else None


def _cache_put(key, value: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(value))


# ------------------------------------------------------------------ cost log
_TABLE_READY = False


def _log(task, prompt_version, meta: CallMeta | None, ok: bool, error: str | None, model: str):
    """Best effort: a logging failure must never break the pipeline."""
    global _TABLE_READY
    try:
        from readmit import db

        if not _TABLE_READY:
            db.ensure_schemas()
            db.execute("""create table if not exists ops.llm_calls (
                ts timestamptz default now(), task text, prompt_version text, model text, cached boolean,
                ok boolean, error text, latency_ms int, input_tokens int, output_tokens int,
                cost_usd double precision, repaired boolean)""")
            _TABLE_READY = True
        db.execute(
            """insert into ops.llm_calls (task, prompt_version, model, cached, ok, error, latency_ms,
                      input_tokens, output_tokens, cost_usd, repaired)
                      values (:task, :pv, :model, :cached, :ok, :error, :lat, :itok, :otok, :cost, :rep)""",
            task=task,
            pv=prompt_version,
            model=model,
            cached=bool(meta and meta.cached),
            ok=ok,
            error=(error or "")[:500] or None,
            lat=meta.latency_ms if meta else None,
            itok=meta.input_tokens if meta else None,
            otok=meta.output_tokens if meta else None,
            cost=meta.cost_usd if meta else None,
            rep=bool(meta and meta.repaired),
        )
    except Exception as e:  # noqa: BLE001 - logging is best effort: warn, never crash the pipeline
        print(f"  [llm] could not write to ops.llm_calls: {str(e)[:120]}", file=sys.stderr)


# ------------------------------------------------------------------ one real call
@traceable(run_type="llm", name="llm_call")
def _call_model(model: str, messages: list[dict], **_trace_info) -> dict:
    """One API call. Traced in LangSmith; `_trace_info` only labels the trace."""
    _wait_turn()
    start = time.monotonic()
    resp = litellm.completion(model=model, messages=messages, timeout=90, response_format={"type": "json_object"})
    latency_ms = int((time.monotonic() - start) * 1000)
    usage = getattr(resp, "usage", None)
    itok = int(getattr(usage, "prompt_tokens", 0) or 0)
    otok = int(getattr(usage, "completion_tokens", 0) or 0)
    try:
        cost = float(litellm.completion_cost(completion_response=resp))
    except Exception:  # noqa: BLE001 - price unknown for this model: record tokens, leave cost empty
        cost = None
    return {
        "content": resp.choices[0].message.content or "",
        "latency_ms": latency_ms,
        "usage_metadata": {"input_tokens": itok, "output_tokens": otok, "total_tokens": itok + otok},
        "cost_usd": cost,
    }


def _call_with_retries(model, messages, trace_info) -> dict:
    last = None
    for attempt in range(MAX_ATTEMPTS_PER_MODEL):
        try:
            return _call_model(
                model,
                messages,
                **trace_info,
                langsmith_extra={
                    "metadata": {**trace_info, "ls_provider": "google", "ls_model_name": model, "attempt": attempt + 1}
                },
            )
        except Exception as e:  # noqa: BLE001 - any API error: back off and retry
            last = e
            time.sleep(2 ** (attempt + 1))
    raise LLMError(f"{model} failed {MAX_ATTEMPTS_PER_MODEL} times: {last}")


# ------------------------------------------------------------------ public API
def complete_json(
    task: str,
    prompt_version: str,
    system: str,
    user: str,
    schema: type[BaseModel],
    model: str | None = None,
    use_cache: bool = True,
) -> tuple[BaseModel, CallMeta]:
    """Ask for JSON matching `schema`. Returns (validated object, call metadata)."""
    models = [model] if model else [settings.llm_model, settings.llm_fallback_model]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    trace_info = {"task": task, "prompt_version": prompt_version}
    errors = []
    if use_cache:  # any model's cached answer wins: never re-call a failing primary for a request already answered
        cached_model = next((m for m in models if _cache_get(_cache_key(m, messages))), None)
        if cached_model:
            models = [cached_model]

    for m in models:
        key = _cache_key(m, messages)
        hit = _cache_get(key) if use_cache else None
        try:
            if hit:
                raw = hit
                meta = CallMeta(
                    m, True, 0, raw["usage_metadata"]["input_tokens"], raw["usage_metadata"]["output_tokens"], 0.0
                )
            else:
                raw = _call_with_retries(m, messages, trace_info)
                meta = CallMeta(
                    m,
                    False,
                    raw["latency_ms"],
                    raw["usage_metadata"]["input_tokens"],
                    raw["usage_metadata"]["output_tokens"],
                    raw["cost_usd"],
                )
            try:
                parsed = schema.model_validate(json.loads(raw["content"]))
            except (json.JSONDecodeError, ValidationError) as e:
                # one repair attempt: show the model its own output and the error
                repair = messages + [
                    {"role": "assistant", "content": raw["content"]},
                    {
                        "role": "user",
                        "content": f"That output was invalid: {str(e)[:800]}\n"
                        "Return ONLY valid JSON that matches the required format.",
                    },
                ]
                raw = _call_with_retries(m, repair, {**trace_info, "repair": True})
                parsed = schema.model_validate(json.loads(raw["content"]))
                meta.repaired = True
                meta.input_tokens += raw["usage_metadata"]["input_tokens"]
                meta.output_tokens += raw["usage_metadata"]["output_tokens"]
                meta.latency_ms += raw["latency_ms"]
            if not hit:
                _cache_put(key, raw)  # only cache answers that validated
            _log(task, prompt_version, meta, True, None, m)
            return parsed, meta
        except Exception as e:  # noqa: BLE001 - try the next model
            errors.append(f"{m}: {e}")
            _log(task, prompt_version, None, False, str(e), m)
    raise LLMError(" | ".join(errors))


def load_prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text()
