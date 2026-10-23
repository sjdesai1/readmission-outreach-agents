"""Prompt A/B test: two versions of one prompt on the same eval cases, compared case by case.

    make ab                                   # write_message v10 (A) vs v11 (B), turn + red-team cases
    make ab task=review_message a=v3 b=v4     # any prompt, any two versions

Why paired: both versions answer exactly the same patient messages, so each case is its own control and the
noise from case difficulty cancels out. With ~70 cases, an unpaired comparison couldn't see small differences.

Hypothesis for v11 (written before running): shorter messages (fewer words, lower reading grade) with no loss
in pass rate and no more rewrites. A change ships only if the primary metric doesn't get worse.

  primary    pass rate on turn + red-team evals; McNemar exact test on the cases where A and B disagree
  secondary  words per message, reading grade (Flesch-Kincaid, English messages), drafts sent back for a
             rewrite, advisory notes from the reviewer; paired mean difference with a bootstrap 95% CI and a
             Wilcoxon signed-rank test
Several secondary metrics are tested, so a lone p just under 0.05 is weak evidence: look at the intervals.
LLM agents only; every call is cached, so rerunning either arm is free.
"""

import argparse
import asyncio
import json
import re
from datetime import datetime

import numpy as np
from scipy.stats import binomtest, wilcoxon

from readmit.config import REPO_ROOT, settings
from readmit.evals.turns import _use_eval_database, load_cases, run_case

DATASETS = ("turns", "redteam")
SPANISH = re.compile(r"\b(hola|usted|su|gracias|visita|cita)\b", re.IGNORECASE)


# ------------------------------------------------------------------ text metrics (pure)
def syllables(word: str) -> int:
    groups = re.findall(r"[aeiouy]+", word.lower())
    n = len(groups) - (1 if word.lower().endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def fk_grade(text: str) -> float | None:
    """Flesch-Kincaid grade level (English). None for Spanish or empty text."""
    if not text or SPANISH.search(text):
        return None
    words = re.findall(r"[A-Za-z']+", text)
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    if not words:
        return None
    return 0.39 * len(words) / sentences + 11.8 * sum(map(syllables, words)) / len(words) - 15.59


def word_count(text: str | None) -> int | None:
    return len(re.findall(r"\w+", text)) if text else None


# ------------------------------------------------------------------ paired statistics (pure)
def mcnemar(a_pass: list[bool], b_pass: list[bool]) -> dict:
    """Exact McNemar: of the cases where A and B disagree, is B winning more often than chance?"""
    b_only = sum(b and not a for a, b in zip(a_pass, b_pass, strict=True))
    a_only = sum(a and not b for a, b in zip(a_pass, b_pass, strict=True))
    n = a_only + b_only
    p = binomtest(b_only, n, 0.5).pvalue if n else 1.0
    return {
        "a_rate": float(np.mean(a_pass)),
        "b_rate": float(np.mean(b_pass)),
        "a_only": a_only,
        "b_only": b_only,
        "p": float(p),
    }


def paired(a: list[float], b: list[float], seed: int = 7) -> dict | None:
    """Mean of B minus A over cases with both values, a bootstrap 95% CI and a Wilcoxon p."""
    pairs = [(x, y) for x, y in zip(a, b, strict=True) if x is not None and y is not None]
    if len(pairs) < 5:
        return None
    d = np.array([y - x for x, y in pairs], float)
    rng = np.random.default_rng(seed)
    boots = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)]
    p = float(wilcoxon(d).pvalue) if np.any(d != 0) else 1.0
    return {
        "n": len(d),
        "a_mean": float(np.mean([x for x, _ in pairs])),
        "b_mean": float(np.mean([y for _, y in pairs])),
        "diff": float(d.mean()),
        "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
        "p": p,
    }


# ------------------------------------------------------------------ running
def run_arm(task: str, version: str, cases: list[dict]) -> list[dict]:
    from readmit.agents import llm_agents

    llm_agents.PROMPTS[task] = version
    rows = []
    for c in cases:
        r = asyncio.run(run_case(c, llm_agents.LLM_AGENTS))
        f = r["facts"]
        text = f.get("drafted")
        rows.append(
            {
                "id": c["id"],
                "passed": r["passed"],
                "words": word_count(text),
                "grade": fk_grade(text),
                "rewritten": float(f.get("writer_calls", 0) > 1) if text else None,
                "advisory": float(f.get("advisory", False)) if text else None,
                "text": text,
            }
        )
        print(f"  {version}  {'pass' if r['passed'] else 'FAIL'}  {c['id']}", flush=True)
    return rows


def compare(a: list[dict], b: list[dict]) -> dict:
    out = {"primary": mcnemar([x["passed"] for x in a], [y["passed"] for y in b])}
    for k in ("words", "grade", "rewritten", "advisory"):
        out[k] = paired([x[k] for x in a], [y[k] for y in b])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="write_message")
    ap.add_argument("--a", default="v10")
    ap.add_argument("--b", default="v11")
    args = ap.parse_args()
    _use_eval_database()
    cases = [c for name in DATASETS for c in load_cases(REPO_ROOT / "evals" / f"{name}.jsonl")]
    from readmit.llm import usage_since

    started = datetime.now().astimezone()
    arm_a = run_arm(args.task, args.a, cases)
    arm_b = run_arm(args.task, args.b, cases)
    r = compare(arm_a, arm_b)

    p = r["primary"]
    print(f"\n  {args.task}: {args.a} (A) vs {args.b} (B) on {len(cases)} cases, paired")
    print(f"  primary  pass rate  A {p['a_rate']:.0%}  B {p['b_rate']:.0%}   "
          f"only A passed {p['a_only']}, only B passed {p['b_only']}, McNemar p = {p['p']:.3f}")  # fmt: skip
    print(f"\n  {'secondary':<11} {'n':>3} {'A':>7} {'B':>7} {'B - A':>8} {'95% CI':>18} {'p':>7}")
    labels = {"words": "words", "grade": "grade", "rewritten": "rewrites", "advisory": "advisory"}
    for k, label in labels.items():
        m = r[k]
        if not m:
            print(f"  {label:<11} too few cases")
            continue
        pct = k in ("rewritten", "advisory")

        def f(x, pct=pct):
            return f"{x:.0%}" if pct else f"{x:.1f}"

        print(
            f"  {label:<11} {m['n']:>3} {f(m['a_mean']):>7} {f(m['b_mean']):>7} {f(m['diff']):>8}"
            f" [{f(m['ci95'][0])}, {f(m['ci95'][1])}]".rjust(19)
            + f" {m['p']:>7.3f}"
        )
    worse = p["b_only"] < p["a_only"]
    print(f"\n  verdict: {'B loses cases A passed: do not ship' if worse else 'B is not worse on the primary metric'}")
    u = usage_since(started)
    if u:
        print(f"  LLM: {u['calls']} calls ({u['cache_hits']} cache hits), ${u['cost_usd']:.4f}")

    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    report = {"task": args.task, "a": args.a, "b": args.b, "comparison": r, "arm_a": arm_a, "arm_b": arm_b}
    file = out / f"ab_{args.task}_{args.a}_vs_{args.b}.json"
    file.write_text(json.dumps(report, indent=2, default=str))
    print(f"  saved {file.relative_to(REPO_ROOT)}")

    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("prompt-ab")
    with mlflow.start_run(run_name=f"{args.task} {args.a} vs {args.b}"):
        mlflow.log_params({"task": args.task, "a": args.a, "b": args.b, "cases": len(cases)})
        mlflow.log_metrics({"a_pass": p["a_rate"], "b_pass": p["b_rate"], "mcnemar_p": p["p"]})
        for k in labels:
            if r[k]:
                mlflow.log_metrics({f"{k}_diff": r[k]["diff"], f"{k}_p": r[k]["p"]})
        mlflow.log_dict(report, file.name)


if __name__ == "__main__":
    main()
