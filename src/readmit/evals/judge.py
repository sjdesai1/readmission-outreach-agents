"""LLM judge: a second model grades the messages patients actually got, on a fixed rubric.

    make eval-judge                    # messages from the last LLM eval runs (turns, red team, conversations)
    make eval-judge agents=rule-based  # the template messages from the rule-based runs: the baseline
    make eval-judge limit=20           # a quick look

Reads the reports the evals saved (data/reports/*_<agents>.json): run those first. Each message sent to a patient
becomes one item: the expected language, the channel, the conversation before it, the message, and the facts it
may state (turn and red-team cases only: offered times, a booked visit).

The rubric (prompts/judge_message_v2.md) is seven yes / no / n/a criteria, each with a reason written before the
verdict: answers the patient, clear next step, warm and respectful tone, plain language, no medical advice,
language quality (Spanish: natural, "usted"), consistent with the facts. Yes/no instead of a 1-10 score: scales
drift toward "8 out of 10" and are hard to check against a person; a yes/no can be.

Known judge biases, and what this does about them:
  self-preference   the judge model (LLM_JUDGE_MODEL) is not the writer's model
  verbosity         the rubric says length earns nothing; the report compares the length of passing and failing
                    messages so a bias toward long messages would show
  leniency          milestone 6 step 2 measures the judge against a person's labels before its scores are trusted

Results: pass rate per criterion (n/a excluded), share of messages passing every criterion, by source, the
failures with the judge's reasons, LLM cost. Saved to data/reports/judge_<agents>.json (the input for labeling)
and logged to MLflow (experiment "judge").
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from readmit.config import REPO_ROOT, settings

PROMPT = "v2"  # v2: earlier messages as context; declining what the system must not do still answers
CRITERIA = (
    "answers_the_patient",
    "clear_next_step",
    "warm_respectful_tone",
    "plain_language",
    "no_medical_advice",
    "language_quality",
    "consistent_with_facts",
)
LANGUAGE = {"C1": "English", "C2": "Spanish"}  # the fixture patients; a language change is read from the facts
CONTEXT = 4  # messages before the graded one
REPORTS = REPO_ROOT / "data" / "reports"


class Verdict(BaseModel):
    reason: str = Field(max_length=400)
    verdict: Literal["yes", "no", "n/a"]


class Judgment(BaseModel):
    answers_the_patient: Verdict
    clear_next_step: Verdict
    warm_respectful_tone: Verdict
    plain_language: Verdict
    no_medical_advice: Verdict
    language_quality: Verdict
    consistent_with_facts: Verdict


# ------------------------------------------------------------------ items (pure)
def items_from_turns(report: dict, cases: list[dict], source: str) -> list[dict]:
    """One item per turn case whose last outbound message was written by the writer and sent."""
    by_id = {c["id"]: c for c in cases}
    items = []
    for r in report["results"]:
        f, case = r["facts"], by_id.get(r["id"])
        if not case or not f.get("drafted") or f.get("action") not in ("sent", "scheduled") or not f.get("sent"):
            continue
        conversation = list(f.get("before") or [])  # e.g. the offer "the second one" refers to
        if case.get("reply"):
            conversation.append({"from": "patient", "channel": case["channel"], "text": case["reply"]})
        items.append(
            {
                "id": f"{source}:{r['id']}",
                "source": source,
                "group": r["category"],
                "language": f.get("language") or LANGUAGE[case["case"]],
                "channel": case.get("channel") or "first message",
                "conversation": conversation,
                "message": f["sent"][-1],
                "facts": f.get("given") or None,
            }
        )
    return items


def items_from_conversations(report: dict) -> list[dict]:
    """One item per AI message in each conversation, with the messages before it as context."""
    items = []
    for r in report["results"]:
        msgs = r.get("transcript") or []
        for i, m in enumerate(msgs):
            if m["author"] != "ai":
                continue
            before = msgs[max(0, i - CONTEXT) : i]
            replied = any(b["author"] == "patient" for b in msgs[:i])
            # switches_language asks for Spanish in her first reply; every message after it should be Spanish
            language = "Spanish" if r["persona"] == "switches_language" and replied else LANGUAGE[r["case"]]
            items.append(
                {
                    "id": f"conversation:{r['persona']}:{r['case']}:{r['seed']}:{i}",
                    "source": "conversations",
                    "group": r["persona"],
                    "language": language,
                    "channel": m["channel"],
                    "conversation": [{"from": b["author"], "channel": b["channel"], "text": b["body"]} for b in before],
                    "message": m["body"],
                    "facts": None,
                }
            )
    return items


def dedupe(items: list[dict]) -> list[dict]:
    """The same message in the same context is graded once (fixed templates repeat across conversations)."""
    seen, out = set(), []
    for it in items:
        key = (it["language"], it["message"], json.dumps(it["conversation"][-1:], sort_keys=True))
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ------------------------------------------------------------------ scoring (pure)
def words(text: str) -> int:
    return len(text.split())


def summarize(judged: list[dict]) -> dict:
    """judged: items with 'verdicts' {criterion: 'yes'|'no'|'n/a'} (items the judge couldn't grade are skipped)."""
    graded = [j for j in judged if j.get("verdicts")]

    def rate(js, criterion):
        v = [j["verdicts"][criterion] for j in js if j["verdicts"].get(criterion) in ("yes", "no")]
        return {"yes": v.count("yes"), "graded": len(v), "rate": v.count("yes") / len(v) if v else None}

    def all_pass(js):
        ok = [all(v != "no" for v in j["verdicts"].values()) for j in js]
        return {"passed": sum(ok), "total": len(ok), "rate": sum(ok) / len(ok) if ok else None}

    by_source = defaultdict(list)
    for j in graded:
        by_source[j["source"]].append(j)
    passing = [words(j["message"]) for j in graded if all(v != "no" for v in j["verdicts"].values())]
    failing = [words(j["message"]) for j in graded if any(v == "no" for v in j["verdicts"].values())]
    return {
        "messages": len(judged),
        "graded": len(graded),
        "all_criteria": all_pass(graded),
        "by_criterion": {c: rate(graded, c) for c in CRITERIA},
        "by_source": {s: all_pass(js) for s, js in sorted(by_source.items())},
        "words": {  # a judge biased toward length would pass longer messages
            "passing_mean": sum(passing) / len(passing) if passing else None,
            "failing_mean": sum(failing) / len(failing) if failing else None,
        },
    }


def failures(judged: list[dict]) -> list[dict]:
    return [
        {"id": j["id"], "criterion": c, "reason": j["reasons"][c], "message": j["message"]}
        for j in judged
        if j.get("verdicts")
        for c in CRITERIA
        if j["verdicts"][c] == "no"
    ]


def enforce_na(item: dict, verdicts: dict) -> dict:
    """Two criteria apply only in some contexts, so code decides, not the judge: with no facts given there's
    nothing to be consistent with; with no patient message since our last one there's nothing to answer."""
    out = dict(verdicts)
    if not item.get("facts"):
        out["consistent_with_facts"] = "n/a"
    if (item.get("conversation") or [{}])[-1].get("from") != "patient":
        out["answers_the_patient"] = "n/a"
    return out


# ------------------------------------------------------------------ run log (for the dashboard)
def record_run(kind: str, agents: str | None, messages: int, pass_rate: float | None, detail: dict) -> None:
    """One row per judge run or judge check in ops.judge_runs (the eval database), read by Grafana."""
    from readmit import db

    db.ensure_schemas()
    with db.transaction() as c:
        db.rows(
            c,
            """create table if not exists ops.judge_runs (
                   ts timestamptz not null default now(), kind text not null, agents text, judge_model text,
                   prompt text, messages int, pass_rate double precision, detail jsonb)""",
        )
        db.rows(
            c,
            """insert into ops.judge_runs (kind, agents, judge_model, prompt, messages, pass_rate, detail)
               values (:kind, :agents, :model, :prompt, :n, :rate, cast(:detail as jsonb))""",
            kind=kind,
            agents=agents,
            model=settings.llm_judge_model,
            prompt=PROMPT,
            n=messages,
            rate=pass_rate,
            detail=json.dumps(detail, default=str),
        )


# ------------------------------------------------------------------ judging
def judge(item: dict) -> dict:
    """Grade one item. Adds verdicts and reasons, or an error (the item is then left out of the rates)."""
    from readmit import llm

    user = {k: item[k] for k in ("language", "channel", "conversation", "message")}
    if item.get("facts"):
        user["facts"] = item["facts"]
    try:
        j, _ = llm.complete_json(
            task="judge_message",
            prompt_version=PROMPT,
            system=llm.load_prompt(f"judge_message_{PROMPT}"),
            user=json.dumps(user, ensure_ascii=False, indent=1),
            schema=Judgment,
            model=settings.llm_judge_model,
        )
    except llm.LLMError as e:
        return {**item, "verdicts": None, "error": str(e)[:200]}
    d = j.model_dump()
    return {
        **item,
        "verdicts": enforce_na(item, {c: d[c]["verdict"] for c in CRITERIA}),
        "reasons": {c: d[c]["reason"] for c in CRITERIA},
    }


def collect(agents: str, sources: list[str]) -> list[dict]:
    from readmit.evals.turns import load_cases

    items: list[dict] = []
    for source in sources:
        name = {"turns": "turn_evals", "redteam": "redteam", "conversations": "conversation_evals"}[source]
        path = REPORTS / f"{name}_{agents}.json"
        if not path.exists():
            print(f"  no {path.name}: run the {source} evals first; skipped")
            continue
        report = json.loads(path.read_text())
        if source == "conversations":
            items += items_from_conversations(report)
        else:
            dataset = "turns" if source == "turns" else "redteam"
            items += items_from_turns(report, load_cases(REPO_ROOT / "evals" / f"{dataset}.jsonl"), source)
    return dedupe(items)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="llm", choices=["llm", "rule-based"])
    ap.add_argument("--sources", default="turns,redteam,conversations")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    from readmit import llm
    from readmit.evals.turns import _use_eval_database

    _use_eval_database()
    items = collect(args.agents, args.sources.split(","))
    if args.limit:
        items = items[: args.limit]
    if not items:
        raise SystemExit("nothing to judge")
    started = datetime.now().astimezone()
    print(f"  judging {len(items)} messages from the {args.agents} runs with {settings.llm_judge_model}", flush=True)
    judged = []
    for n, item in enumerate(items, 1):
        judged.append(judge(item))
        if n % 20 == 0:
            print(f"    {n}/{len(items)}", flush=True)
    s = summarize(judged)

    a = s["all_criteria"]
    print(f"\n  {a['passed']}/{a['total']} messages pass every criterion ({(a['rate'] or 0):.0%})")
    if s["graded"] < len(items):
        errors = Counter(j["error"][:120] for j in judged if j.get("error"))
        print(f"  {len(items) - s['graded']} of {len(items)} messages not graded; judge errors:")
        for e, n in errors.most_common(3):
            print(f"    {n}x {e}")
    print("\n  by criterion (n/a excluded)")
    for c, v in s["by_criterion"].items():
        shown = f"{v['rate']:.0%}" if v["rate"] is not None else "-"
        print(f"    {c:<24} {v['yes']:>3}/{v['graded']:<3} {shown:>5}")
    print("\n  by source")
    for src, v in s["by_source"].items():
        print(f"    {src:<14} {v['passed']:>3}/{v['total']:<3}")
    w = s["words"]
    if w["passing_mean"] and w["failing_mean"]:
        print(f"\n  words: passing messages {w['passing_mean']:.0f}, failing {w['failing_mean']:.0f}")
    fails = failures(judged)
    if fails:
        print("\n  failures")
        for f in fails[:15]:
            print(f"    {f['id']}  {f['criterion']}: {f['reason'][:160]}")
            print(f"      {f['message'][:140]}")
        if len(fails) > 15:
            print(f"    ... {len(fails) - 15} more in the report")
    u = llm.usage_since(started)
    if u:
        print(f"\n  LLM: {u['calls']} calls ({u['cache_hits']} cache hits), ${u['cost_usd']:.4f}")

    record_run(
        "judge",
        args.agents,
        s["graded"],
        a["rate"],
        {c: v["rate"] for c, v in s["by_criterion"].items()} | {"by_source": s["by_source"]},
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = {"agents": args.agents, "judge_model": settings.llm_judge_model, "prompt": PROMPT, "summary": s,
           "items": judged}  # fmt: skip
    (REPORTS / f"judge_{args.agents}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"  saved data/reports/judge_{args.agents}.json")

    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("judge")
    with mlflow.start_run(run_name=f"judge {args.agents}"):
        mlflow.log_params({"agents": args.agents, "judge_model": settings.llm_judge_model, "prompt": PROMPT,
                           "messages": s["messages"]})  # fmt: skip
        mlflow.log_metric("all_criteria_rate", a["rate"] or 0)
        for c, v in s["by_criterion"].items():
            if v["rate"] is not None:
                mlflow.log_metric(c, v["rate"])


if __name__ == "__main__":
    main()
