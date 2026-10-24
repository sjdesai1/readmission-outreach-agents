# Milestone 6: LLM judge and judge assessment

Goal: grade the messages patients actually get, and know how far to trust the grader. Every number below comes from a command in this repo.

```bash
make eval-judge                   # grade the messages from the last LLM eval runs
make eval-judge agents=rule-based # the template messages: the baseline
make judge-label                  # label 20 judged messages yourself (blind to the judge)
make judge-check                  # planted errors + agreement with your labels
make ab judge=1                   # prompt A/B with judge scores as a secondary metric
```

## 1. The judge

A second model (LLM_JUDGE_MODEL, gemini-3.1-flash-lite; the writer is gemini-3.5-flash-lite) grades each message on seven yes / no / n/a criteria, writing a reason before each verdict: answers the patient, clear next step, warm and respectful tone, plain language, no medical advice, language quality (Spanish: natural, "usted"), consistent with the facts.

- Yes/no per criterion instead of a 1-10 score: scores drift toward "8", and a yes/no can be checked against a person
- Inputs: the conversation before the message, the message, and the facts it may state (offered times, a booked visit)
- Code, not the judge, marks two criteria n/a: facts when none are given, "answers the patient" when the patient hasn't written since our last message
- Graded: messages the writer drafted and sent (turn and red-team cases) and every AI message in the conversation evals; identical messages in the same context once

| Run | Rubric | LLM agents | Rule-based templates |
|---|---|---|---|
| first | v1 | 86/96 (90%) | 69/96 (72%) |
| final | v2 | 95/96 (99%) | 88/96 (92%) |

- v1's failures were mostly the judge's: it never saw the offer "the second one" referred to, it failed correct refusals (no diagnosis by text), and it judged facts against the conversation when no facts were given. v2 fixes the inputs and the wording
- The cost of v2: the gap between agents and templates narrowed (90 vs 72 became 99 vs 92). The remaining template failures are real: a copay worry, a standing preference, a symptom inside a yes, a video request, all answered with "didn't understand"
- The one LLM failure is real: Jose asked for video, can't do video, and the confirmation for a clinic visit never says so or mentions the ride
- No length bias: passing and failing messages are about as long (43 vs 40 words with v1)
- gemini-3.5-flash was the first choice of judge; its free daily quota ran out after 17 calls

## 2. Is the judge right?

Checked on data it wasn't tuned on.

Planted errors: messages the judge passed, broken on purpose, one flaw each, aimed at one criterion; up to 8 per flaw. Untouched originals measure false alarms.

| Flaw | Caught |
|---|---|
| wrong time | 8/8 |
| medical advice | 8/8 |
| pressure | 8/8 |
| jargon | 8/8 |
| ignores the patient | 5/8 |
| next step removed | 4/8 |
| broken English or "tú" | 4/8 |
| total | 45/56 (80%) |
| false alarms on 15 untouched messages | 0 |

- The safety-critical flaws are caught every time
- Weak spot: grammar and Spanish formality. "you visit are set" and "tu visita" passed half the time
- Some "next step removed" misses are ambiguous plants: "a nurse will follow up with you" still says what happens next
- Planted set v2 requires 2+ swapped words; v1 let one-word changes through (44/56 then, bad language 2/8)

Agreement with a person: 20 messages (LLM and template, judge passes and failures mixed), labeled blind to the judge's verdict, saved in evals/judge_labels.jsonl.

- Pass/fail agreement 18/20, Cohen's kappa 0.80
- Never stricter than the person; lenient twice, both the same pattern: "a nurse will follow up" in reply to a prompt-leak and an all-caps request. From the text the reply looks polite; the judge can't know no nurse task exists
- With 20 labels the interval on kappa is wide; what the disagreements are matters more than the number

## 3. Wiring

- Dashboard: a "Message quality" row in Grafana, from a new data source on the eval database: the latest LLM run's share of messages passing every criterion, and every judge run and check (planted caught, false alarms, kappa). Each run writes a row to ops.judge_runs
- `make ab judge=1`: "judge ok" as a paired secondary metric

A/B rerun, write_message v10 vs v11, 87 cases (the 72 from milestone 5 plus language and red-team cases added since):

| Metric | v10 | v11 | B minus A (95% CI) | p |
|---|---|---|---|---|
| pass rate (primary) | 100% | 100% | no case lost | 1.00 |
| words per message | 40.7 | 40.5 | -0.1 [-2.6, 2.4] | 0.83 |
| rewrites | 7% | 9% | +2% [-9%, 14%] | 0.71 |
| judge ok | 100% | 100% | 0 | 1.00 |

- The milestone 5 result (v11 2.9 words shorter, p = 0.008) did not replicate. The writer's inputs changed since (interpreter v6, new cases), and the effect was small to begin with. v11 stays: it loses nothing. The lesson: a small secondary effect from one run is a hypothesis, not a finding
- The judge can't separate v10 and v11: both at 100%. It catches bad messages, not the difference between two good ones

## What a person found that neither the evals nor the judge did

Labeling 20 messages surfaced bugs no automated check could see:

- The rule-based "question" template promises "a nurse will follow up" without creating a nurse task; the LLM writer sometimes does the same
- The LLM writer promised "a free ride" and "an interpreter will accompany you": services nothing arranged
- The rule-based barrier template offers video to a patient who can't do video
- A heart failure patient's "my legs are a bit more puffy" inside a yes was booked, not sent to a nurse (rule-based fallback)

All four are on the fix list for the next PR. The shared lesson: promises in a message must match something on file, and only code can check that.

## What this milestone shows

- An LLM judge is a measurement instrument: check it against known errors and a person before trusting its numbers, and report where it's weak
- Tuning a judge on the data it then scores inflates it; the check uses planted errors and labels it never saw
- The judge grades wording; code checks what wording can't prove (promises, formality, facts) ([ADR 0011](decisions/0011-judge-grades-wording-code-checks-promises.md))
- Reading real messages is still the best bug finder
