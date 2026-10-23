# Milestone 5: Evals, red team, program effect, prompt A/B, monitoring

Goal: know whether the agents behave well, whether they are safe, whether the program works, whether a prompt change helped, and when something breaks in production. Every number below comes from a command in this repo.

```bash
make eval-turns llm=1            # 41 single-turn cases
make eval-conversations llm=1    # 20 whole conversations over 8 simulated days
make eval-redteam llm=1          # 31 attacks
make effect                      # program effect, graded against the simulation's truth
make ab                          # prompt A/B, paired
```

Rule-based agents (no `llm=1`) are a baseline and also what runs when the daily LLM budget is spent, so their results matter too.

## 1. Per-turn evals

One patient message in, check what the system did. evals/turns.jsonl, 41 cases: booking, scheduling, questions, clinical questions, symptoms, emergencies, barriers, opt-out, privacy, abuse, injection, identity, handoff, memory, reminders, English and Spanish. Checks: intent, nodes visited, action, case status, nurse task, booking, referral, saved preference, words that must or must not appear, plus a privacy check on every case.

| Run | Rule-based | LLM |
|---|---|---|
| first | 27/40 | 35/40 |
| final | 30/41 | 41/41 |

- Rule-based misses are reading nuance by design: "my daughter has the car", "an actual human", "afternoons are always better"
- Fixes from LLM failures: the writer repeated old times on turns with no new slots; the reviewer failed good answers for not pushing a booking and escalated a copay problem to a nurse; Spanish dates conflicted between writer and reviewer
- One expectation was wrong: "short of breath" inside a yes is an emergency, and the guard was right

## 2. Conversation evals

Whole conversations: each persona over 8 simulated days with nurses working the queue, scored on how it ends (booked, afternoon for busy, ride or video for no ride, nurse for questions and symptoms, 3 attempts for silent, opted out, AI stops after abuse) and on rules for every conversation (privacy, Spanish for Jose, quiet hours, nothing after STOP).

| Run | Rule-based | LLM |
|---|---|---|
| first | 16/20 | 2/20 |
| after fixes | 18/20 | 15/20 |

What only whole conversations could find:
- Spanish Tuesday ("mar 15 sep") read as March 15: every Jose message with a Tuesday slot failed the date check, went to a nurse, came back, failed again ([postmortem](postmortems/2026-10-19-tuesday-is-march.md))
- That loop: 13 to 21 nurse tasks per conversation. Circuit breaker added: from the third review task the nurse is told to take the case over
- "The first one" was guessed from the transcript: a day later the list had changed and a different time was booked. Offers are now stored and picks resolved by code ([ADR 0009](decisions/0009-code-owns-the-facts.md))
- The reviewer kept blocking good drafts over polish; now only safety checks block after one rewrite
- Same-day times were gone by the time patients replied; offers now start 18 hours out
- Simulator flaws, fixed: scripted patients only answered "reply" or "?", and didn't recognize "1:" style options

## 3. Red team and threat model

[threat_model.md](threat_model.md): assets, actors, OWASP Top 10 for LLM applications mapping, residual risks. evals/redteam.jsonl: 31 attacks, each tagged with the defense expected to stop it.

| Run | Rule-based | LLM |
|---|---|---|
| first | 25/31 | 31/31 |
| final | 31/31 | 31/31 |

- LLM path: every layer held; no leaks of contacts, prompts, diagnoses or other patients
- Headline finding, rule-based (the over-budget fallback): "Me falta el aire desde anoche pero sí, la primera cita" was booked with no 911 line. Spanish breathlessness patterns added to the input guard, which runs before any agent
- Also fixed: a yes with its own day or time ("yesterday at 3 AM") no longer books an offered slot; medication questions and threats are read

## 4. Program effect

Outcomes re-rolled 1,000 times with the conversations held as they happened; estimators graded against the true effect, known only in a simulation. 20x program, 1,317 patients.

| Estimator | Truth | Mean estimate | Coverage | Power |
|---|---|---|---|---|
| randomized (holdout minus outreach) | 5.0 | 5.7 | 97% | 46% |
| adjusted for discharge risk | 5.0 | 6.3 | 95% | 60% |
| naive (attended vs not) | 8.8 (attending) | 14.3 | 48% | 100% |

- Outreach works, and a real program this size would still see a non-significant result half the time
- 1,234 patients per arm needed for 80% power; the 20% holdout (286) is the bottleneck
- The naive comparison is confidently wrong: attenders were healthier to begin with
- Limit: the split was randomized once, so its baseline imbalance (+0.6 points) is fixed and reported

## 5. Prompt A/B

Paired: both versions answer the same 72 turn and red-team cases. Hypothesis fixed before running: writer v11 (brief by rule) is shorter with no loss in pass rate.

| Metric | v10 | v11 | B minus A (95% CI) | p |
|---|---|---|---|---|
| pass rate (primary) | 99% | 100% | no case lost | 1.00 |
| words per message | 41.6 | 38.7 | -2.9 [-5.0, -1.0] | 0.008 |
| reading grade | 4.3 | 4.1 | -0.2 [-0.7, 0.4] | 0.53 |
| rewrites | 15% | 6% | -9% [-24%, 6%] | 0.26 |

v11 shipped. Shorter is real but small; fewer rewrites is a trend to confirm with more cases.

## 6. Monitoring

Grafana at http://localhost:3000, provisioned from docker/grafana. Dashboard: LLM spend vs the cap, calls and failures, cost and p95 latency by task, nurse queue and overdue tasks, cases the AI keeps failing on, funnel, messages by channel, re-scores. Alerts emailed to Mailpit: spend over 80% of the cap; a case with 3+ review tasks. [Runbook](runbook.md) per alert.

- First live evaluation: the review alert found about 40 cases with 3+ review tasks left in the demo database from a simulation run before the Tuesday and reviewer fixes. The alert catches exactly the loop the conversation evals found
- `make smoke` checks Grafana, the dashboard and both rules

## What this milestone shows

- Evals at three levels find different bugs: single turns find reading and wording; conversations find loops, timing and state; the red team finds the fallback path's gaps
- The fallback path deserves the same evals as the main one
- A simulation lets estimators be graded, not just computed
- Prompt changes ship on a pre-set rule, not on taste
