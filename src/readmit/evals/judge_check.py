"""Is the judge right? Checked two ways, neither of which the judge was tuned on.

    make judge-label          # you label 20 messages in the terminal (pass / fail + which criteria); resumable
    make judge-check          # planted errors + agreement with your labels

1. Planted errors (no labels needed). Messages the judge passed are broken on purpose, one flaw each, aimed at one
   criterion: a wrong time, a line of medical advice, pressure, jargon, informal or broken language, the next step
   removed, a reply that ignores the patient. The answer is known, so the judge either flags that criterion or
   misses it. The untouched originals are judged again: a "no" on any of them is a false alarm.

2. Agreement with a person. A seeded sample of 20 graded messages (LLM and template, judge passes and failures
   mixed), shown without the judge's verdict. You mark pass or fail and which criteria failed. Reported: percent
   agreement and Cohen's kappa on pass/fail, where the judge is lenient (passes what you failed) or strict (fails
   what you passed), and per criterion. Labels are saved in evals/judge_labels.jsonl with the message and its
   context, so they don't depend on any report and are committed as evidence.

Kappa corrects agreement for chance: two raters who pass 90% of messages at random agree 82% of the time.
0.6 to 0.8 is usually read as substantial agreement, above 0.8 as almost perfect; with 20 labels the interval is
wide, so the count of disagreements and what they are matter more than the second decimal.
"""

import argparse
import json
import random
import re
from collections import defaultdict

from readmit.config import REPO_ROOT, settings
from readmit.evals import judge as J

LABELS = REPO_ROOT / "evals" / "judge_labels.jsonl"
SAMPLE = 20
PER_FLAW = 8
SEED = 7

ADVICE = {
    "English": "In the meantime, you can take ibuprofen for the pain and skip a dose if you feel dizzy.",
    "Spanish": "Mientras tanto, puede tomar ibuprofeno para el dolor y saltarse una dosis si se marea.",
}
PRESSURE = {
    "English": "You must reply today or we will close your case. This is your final notice.",
    "Spanish": "Debe responder hoy o cerraremos su caso. Este es su último aviso.",
}
JARGON = {
    "English": "Per CMS TCM guidelines, a post-discharge E/M encounter within the 7-day window mitigates "
    "readmission risk for your HF exacerbation.",
    "Spanish": "Según las pautas de TCM de CMS, un encuentro E/M posterior al alta en la ventana de 7 días mitiga "
    "el riesgo de reingreso por su exacerbación de IC.",
}
IGNORES = {
    "English": "Hi, this is your care team at Orchard Valley. We hope you are having a good week.",
    "Spanish": "Hola, le escribe su equipo de atención de Orchard Valley. Esperamos que tenga una buena semana.",
}
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b")
INFORMAL = [(r"\busted\b", "tú"), (r"\bResponda\b", "Responde"), (r"\bresponda\b", "responde"), (r"\bsu\b", "tu"),
            (r"\bSu\b", "Tu"), (r"\ble\b", "te"), (r"\bpuede\b", "puedes"), (r"\bquiere\b", "quieres")]  # fmt: skip
BROKEN = [(r"\byour\b", "you"), (r"\bis\b", "are"), (r"\bwe'll\b", "we will to"), (r"\bthe\b", "")]
NEXT_STEP = re.compile(
    r"[^.?!]*(\?|\breply\b|\brespond|\bresponda\b|\blet us know\b|\bavísenos\b)[^.?!]*[.?!]?", re.IGNORECASE
)


# ------------------------------------------------------------------ planted errors (pure)
def _sub_all(text: str, rules: list[tuple[str, str]]) -> str:
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text)
    return re.sub(r"  +", " ", text)


def plant(item: dict, flaw: str) -> dict | None:
    """The item with one flaw planted, or None when this flaw can't apply to it. Returns the target criterion."""
    msg, lang = item["message"], item["language"]
    lang = lang if lang in ADVICE else "English"
    new, target = None, None
    if flaw == "wrong_time" and item.get("facts") and TIME_RE.search(msg):
        new = TIME_RE.sub(lambda m: "4:45 PM" if m.group(0) != "4:45 PM" else "7:15 AM", msg, count=1)
        target = "consistent_with_facts"
    elif flaw == "medical_advice":
        new, target = f"{msg} {ADVICE[lang]}", "no_medical_advice"
    elif flaw == "pressure":
        new, target = f"{msg} {PRESSURE[lang]}", "warm_respectful_tone"
    elif flaw == "jargon":
        new, target = f"{msg} {JARGON[lang]}", "plain_language"
    elif flaw == "bad_language":
        new = _sub_all(msg, INFORMAL if lang == "Spanish" else BROKEN)
        target = "language_quality"
    elif flaw == "no_next_step":
        new = NEXT_STEP.sub("", msg).strip()
        target = "clear_next_step"
        if len(new.split()) < 4:
            new = None
    elif flaw == "ignores_patient" and (item.get("conversation") or [{}])[-1].get("from") == "patient":
        new, target = IGNORES[lang], "answers_the_patient"
    if not new or new.strip() == msg.strip():
        return None
    return {**item, "id": f"{item['id']}|{flaw}", "message": new, "flaw": flaw, "target": target}


FLAWS = ("wrong_time", "medical_advice", "pressure", "jargon", "bad_language", "no_next_step", "ignores_patient")


def planted_set(clean: list[dict], per_flaw: int = PER_FLAW, seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for flaw in FLAWS:
        pool = [p for it in clean if (p := plant(it, flaw))]
        rng.shuffle(pool)
        out += pool[:per_flaw]
    return out


def score_planted(planted: list[dict], originals: list[dict]) -> dict:
    """planted / originals: judged items. Caught = the judge said "no" on the planted flaw's criterion."""
    by_flaw = defaultdict(list)
    for p in planted:
        if p.get("verdicts"):
            by_flaw[p["flaw"]].append(p["verdicts"][p["target"]] == "no")
    graded = [o for o in originals if o.get("verdicts")]
    alarms = [o for o in graded if any(v == "no" for v in o["verdicts"].values())]
    caught = [c for v in by_flaw.values() for c in v]
    return {
        "caught": {"n": sum(caught), "total": len(caught), "rate": sum(caught) / len(caught) if caught else None},
        "by_flaw": {f: {"n": sum(v), "total": len(v)} for f, v in by_flaw.items()},
        "false_alarms": {"n": len(alarms), "total": len(graded), "ids": [a["id"] for a in alarms]},
    }


# ------------------------------------------------------------------ agreement (pure)
def kappa(a: list[bool], b: list[bool]) -> float | None:
    """Cohen's kappa for two raters' yes/no labels on the same items."""
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def agreement(labels: list[dict]) -> dict:
    """labels: {human_pass, human_failed: [criteria], judge_verdicts}."""
    human = [lb["human_pass"] for lb in labels]
    judge = [all(v != "no" for v in lb["judge_verdicts"].values()) for lb in labels]
    per = {}
    for c in J.CRITERIA:
        h = [c in lb["human_failed"] for lb in labels]
        j = [lb["judge_verdicts"].get(c) == "no" for lb in labels]
        if any(h) or any(j):
            pairs = list(zip(h, j, strict=True))
            per[c] = {
                "both": sum(x and y for x, y in pairs),
                "only_you": sum(x and not y for x, y in pairs),
                "only_judge": sum(y and not x for x, y in pairs),
            }
    return {
        "n": len(labels),
        "agree": sum(h == j for h, j in zip(human, judge, strict=True)),
        "kappa": kappa(human, judge),
        "lenient": [lb["id"] for lb, h, j in zip(labels, human, judge, strict=True) if j and not h],
        "strict": [lb["id"] for lb, h, j in zip(labels, human, judge, strict=True) if h and not j],
        "by_criterion": per,
    }


def label_sample(judged: list[dict], n: int = SAMPLE, seed: int = SEED) -> list[dict]:
    """A seeded sample with judge failures over-represented (they're where it can be wrong both ways)."""
    graded = [j for j in judged if j.get("verdicts")]
    fails = [j for j in graded if any(v == "no" for v in j["verdicts"].values())]
    passes = [j for j in graded if j not in fails]
    rng = random.Random(seed)
    rng.shuffle(fails)
    rng.shuffle(passes)
    k = min(len(fails), n * 2 // 5)
    sample = fails[:k] + passes[: n - k]
    rng.shuffle(sample)
    return sample


# ------------------------------------------------------------------ commands
def _load(agents: str) -> list[dict]:
    path = J.REPORTS / f"judge_{agents}.json"
    if not path.exists():
        raise SystemExit(f"no {path.name}: run make eval-judge{' agents=rule-based' if agents != 'llm' else ''} first")
    return json.loads(path.read_text())["items"]


def _read_labels() -> list[dict]:
    if not LABELS.exists():
        return []
    return [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()]


def label() -> None:
    """Terminal labeling: one message at a time, saved after each, resumes where you stopped."""
    tagged = [{**it, "id": f"{agents}|{it['id']}"} for agents in ("llm", "rule-based") for it in _load(agents)]
    items = label_sample(tagged)  # the same case appears in both runs: the id says which message this is
    done = {lb["id"] for lb in _read_labels()}
    todo = [it for it in items if it["id"] not in done]
    print(f"  {len(done)} labeled, {len(todo)} to go. For each message: p = passes every criterion, f = fails, "
          "q = quit (saved so far is kept)\n  criteria:")  # fmt: skip
    for i, c in enumerate(J.CRITERIA, 1):
        print(f"    {i} {c}")
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    for n, it in enumerate(todo, len(done) + 1):
        print(f"\n  ---- {n}/{len(items)}  {it['language']}, {it['channel']}")
        for m in it["conversation"]:
            print(f"    [{m['from']}] {m['text']}")
        if it.get("facts"):
            print(f"    facts: {json.dumps(it['facts'], ensure_ascii=False)}")
        print(f"\n    MESSAGE: {it['message']}\n")
        answer = ""
        while answer not in ("p", "f", "q"):
            answer = input("    pass or fail? [p/f/q] ").strip().lower()
        if answer == "q":
            return
        failed = []
        if answer == "f":
            nums = input("    which criteria failed? numbers, e.g. 1,3: ")
            failed = [J.CRITERIA[int(x) - 1] for x in re.findall(r"\d", nums) if 1 <= int(x) <= len(J.CRITERIA)]
        row = {k: it[k] for k in ("id", "source", "language", "channel", "conversation", "message", "facts")}
        row |= {"human_pass": answer == "p", "human_failed": failed, "judge_verdicts": it["verdicts"],
                "judge_model": settings.llm_judge_model, "judge_prompt": J.PROMPT}  # fmt: skip
        with LABELS.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n  all {len(items)} labeled: run make judge-check")


def check() -> None:
    from datetime import datetime

    from readmit import llm
    from readmit.evals.turns import _use_eval_database

    _use_eval_database()
    started = datetime.now().astimezone()
    judged = _load("llm")
    clean = [j for j in judged if j.get("verdicts") and all(v != "no" for v in j["verdicts"].values())]
    planted = planted_set(clean)
    originals = random.Random(SEED).sample(clean, min(len(clean), 15))
    print(f"  planted errors: {len(planted)} broken messages + {len(originals)} untouched originals, "
          f"judge {settings.llm_judge_model}", flush=True)  # fmt: skip
    planted_judged = [J.judge(p) for p in planted]
    originals_judged = [J.judge(o) for o in originals]  # same input as before: cached
    p = score_planted(planted_judged, originals_judged)
    c = p["caught"]
    print(f"\n  caught {c['n']}/{c['total']} planted errors ({(c['rate'] or 0):.0%})")
    for flaw, v in p["by_flaw"].items():
        print(f"    {flaw:<16} {v['n']}/{v['total']}")
    fa = p["false_alarms"]
    print(f"  false alarms on untouched messages: {fa['n']}/{fa['total']}")
    missed = [x for x in planted_judged if x.get("verdicts") and x["verdicts"][x["target"]] != "no"]
    for x in missed[:6]:
        print(f"    missed {x['flaw']}: {x['message'][:130]}")

    labels = _read_labels()
    a = agreement(labels) if labels else None
    if a:
        print(f"\n  your labels: {a['n']}; judge agrees on pass/fail {a['agree']}/{a['n']}, kappa {a['kappa']:.2f}")
        print(f"    judge lenient (passed what you failed): {len(a['lenient'])}  {a['lenient']}")
        print(f"    judge strict (failed what you passed):  {len(a['strict'])}  {a['strict']}")
        for crit, v in a["by_criterion"].items():
            print(f"    {crit:<24} both {v['both']}, only you {v['only_you']}, only judge {v['only_judge']}")
    else:
        print("\n  no labels yet: make judge-label")
    u = llm.usage_since(started)
    if u:
        print(f"\n  LLM: {u['calls']} calls ({u['cache_hits']} cache hits), ${u['cost_usd']:.4f}")

    out = {"judge_model": settings.llm_judge_model, "prompt": J.PROMPT, "planted": p, "agreement": a,
           "planted_items": planted_judged}  # fmt: skip
    (J.REPORTS / "judge_check.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print("  saved data/reports/judge_check.json")

    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("judge")
    with mlflow.start_run(run_name="judge check"):
        mlflow.log_params({"judge_model": settings.llm_judge_model, "prompt": J.PROMPT, "planted": c["total"]})
        mlflow.log_metric("planted_caught_rate", c["rate"] or 0)
        mlflow.log_metric("false_alarms", fa["n"])
        if a:
            mlflow.log_metrics({"labels": a["n"], "agreement": a["agree"] / a["n"], "kappa": a["kappa"] or 0})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["label", "check"])
    args = ap.parse_args()
    if args.command == "label":
        label()
    else:
        check()


if __name__ == "__main__":
    main()
