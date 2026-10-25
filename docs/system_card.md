# System card: readmission risk + outreach agents (v1.0.0)

A system card describes a whole AI system in one place: what it is for, how it is built, how it was tested, where it fails, and what keeps it safe. A model card covers one model; this covers the model, the agents, the guardrails and the people around them.

Everything here runs on synthetic data. No real patient has ever been in this system.

## 1. Purpose

- Intended use: help a hospital medicine team reach recently discharged patients at high risk of a 30-day readmission, and get a follow-up visit booked within days
- Users: the outreach team (nurses) and their patients; the AI handles routine scheduling messages, nurses handle anything clinical
- Success measure: fewer 30-day readmissions in outreach patients than in a randomized holdout; secondary: visits booked and attended, nurse time per case

Out of scope, by design:
- Clinical decisions: no diagnosis, no medication advice, no triage beyond "a nurse calls you" or "call 911"
- Denying or rationing care by risk score: the score decides who gets extra outreach, never who gets less care
- Telling a patient their risk score or prediction
- Real patient data: the system has not been validated, secured or reviewed for real PHI
- Languages other than English and Spanish: those go to a nurse with an interpreter

## 2. How it works

```
Discharges -> notes parsed by an LLM (social factors, every quote checked against the note)
           -> XGBoost 30-day risk, SHAP reasons -> tiers (high = top 10%, medium = next 20%)
           -> 20% of flagged patients held out at random (no outreach): the honest comparison
           -> one outreach case per flagged patient

Each turn (outreach due, or a patient reply):
  code: load context -> input guard (STOP, emergency, family member, identity, language, case with a nurse)
  LLM agents: reply interpreter -> care planner -> scheduler -> message writer -> safety reviewer
  code: output checks (times, promises, formality, length, tone) -> send, or hand to a nurse
  every action through MCP servers over the case store: consent, quiet hours, idempotency enforced there

After discharge: re-scored on an ED visit, a missed prescription fill, and on day 7, 14, 21
```

- Code routes; LLMs make judgments inside nodes ([ADR 0002](decisions/0002-deterministic-routing.md))
- Business rules live behind MCP, not in prompts ([ADR 0003](decisions/0003-rules-behind-mcp.md))
- The LLM never sees names or contact details; code fills them in at send time ([ADR 0004](decisions/0004-llm-never-sees-names.md))
- Code owns the facts of a conversation: offered times, bookings, language ([ADR 0009](decisions/0009-code-owns-the-facts.md), [ADR 0010](decisions/0010-language-is-a-stored-fact.md))
- Models: Gemini flash-lite for the agents (fallback model on failure), a different model for the judge; every agent has a rule-based twin

## 3. Data

- Simulated hospital: 1,000 patients, 3 years of stays, messy physician notes with abbreviations, negations, family history and typos
- A hidden risk formula decides readmissions, so every estimate can be checked against the truth
- Social factors appear only in notes, never in structured columns
- Split by time: train before Mar 2026, test Mar to Jul, live Sep to Nov (246 discharges, 58 flagged, 46 to outreach, 12 holdout)
- Scripted patients (personas): engaged, busy, silent, medication question, symptomatic, opt out, no ride, prompt injection, abusive, Spanish speaker

## 4. Results

Risk model (test set, [docs/02](02_m1_data_and_model.md)):

| Model | AUROC | Recall in top 30% |
|---|---|---|
| LACE (clinical standard) | 0.714 | 0.61 |
| XGBoost, structured + note factors (champion) | 0.804 | 0.68 |
| Ceiling (true risk) | 0.843 | 0.71 |

Re-scoring after discharge ([docs/05](05_m4_rescoring.md)): new signals lift AUROC on day 3 (+0.025) and day 7 (+0.043), both with 95% CIs above zero; nothing significant on day 14 or 21.

Agents ([docs/07](07_m5_evals.md), latest runs):

| Eval | Rule-based | LLM agents |
|---|---|---|
| Single turns | 52/57 | 57/57 |
| Red team (each attack mapped to its defense) | 34/34 | 34/34 |
| Whole conversations, 8 simulated days per persona | see docs/07 | 21/22 |
| Promise checks (every promise backed by a task or referral) | see docs/07 | 44/44 |

LLM judge ([docs/08](08_m6_judge.md)): 45/56 planted errors caught, all safety-critical ones; 0/15 false alarms; agrees with a person on 18/20 messages (Cohen's kappa 0.80, wide interval at n = 20).

Program effect ([docs/07](07_m5_evals.md)), graded against the simulation's truth over 1,000 draws:

| Estimator | Truth | Mean estimate | 95% CI coverage |
|---|---|---|---|
| Randomized (holdout minus outreach) | 5.0 pts | 5.7 | 97% |
| Naive (attended vs not) | 8.8 | 14.3 | 48% |

The naive comparison doubles the effect: patients who attend were healthier to begin with. The holdout is what makes the effect measurable.

Cost: a 22-conversation LLM eval run is about 125 calls, $0.10.

## 5. Safety controls

| Risk | Control | Tested by |
|---|---|---|
| A symptom or emergency gets a scheduling reply | Rule guard before any LLM (911 message + urgent nurse task); symptoms inside a yes still go to a nurse | turn and red-team cases |
| Medical advice | Writer prompt, reviewer, output checks; reviewer's medical-advice finding goes to a nurse | red team, judge |
| A promise nobody keeps ("a nurse will call") | Code checks every nurse, ride, interpreter and video promise against tasks and referrals on file | promise checks, [ADR 0011](decisions/0011-judge-grades-wording-code-checks-promises.md) |
| Wrong time or date | Offers stored; "the first one" resolved by code; every time in a message checked against the offers | [postmortem](postmortems/2026-10-19-tuesday-is-march.md), [ADR 0009](decisions/0009-code-owns-the-facts.md) |
| Health information to the wrong person | Family members get a fixed privacy reply; phone replies need a year-of-birth check, server-side | red team |
| Prompt injection | Patient text only ever read as data; actions only through MCP tools with code-side checks | red team |
| Consent | STOP (English and Spanish) ends everything; only opted-in channels; quiet hours held by the server | conversation evals |
| A language the AI can't read | Code detects it; nurse with an interpreter, urgent if unread | [ADR 0010](decisions/0010-language-is-a-stored-fact.md) |
| Runaway cost or an LLM outage | Daily spend cap and any LLM error fall back to the rule-based agents | [ADR 0008](decisions/0008-spend-cap-falls-back-to-rules.md), [ADR 0013](decisions/0013-llm-outage-falls-back-to-rules.md) |
| The AI loops on a case | From the third review task, the nurse is told to take the case over; Grafana alert | conversation evals, runbook |

Every action is in an audit trail; `make timeline` shows one case's trail in plain words.

## 6. Known limitations and failures

- Slow responders: next-day offers can pass before a slow patient replies, and two no-shows end in no visit (the one conversation-eval failure, busy C1). v2: offer times further out for slow responders
- The judge misses half of planted grammar and Spanish formality errors; formality is checked by code instead, grammar is not
- Note extraction adds no significant AUROC (+0.003, CI crosses zero) at ~1,200 training stays; its value is operational (agents act on the barriers)
- The model is weaker for first-time patients (0.71 vs 0.79 AUROC): it leans on visit history
- The randomized estimate has 46% power at this program size: a real program needs more patients or a longer run
- Without an API key the demo uses keyword rules for note factors, checked against the simulator's own phrasings; they would miss unseen wording on real notes
- Synthetic data and scripted patients: real patients write more variously, and real clinics change
- English and Spanish only; Spanish quality labeled by a non-native speaker

## 7. Monitoring and operations

- Grafana dashboard: LLM spend vs the cap, calls and failures, cost and p95 latency per agent, nurse queue and overdue tasks, cases the AI keeps failing on, funnel, re-scores, judge results
- Alerts emailed (Mailpit locally): spend over 80% of the cap; a case with 3+ review tasks
- [Runbook](runbook.md) per alert; [threat model](threat_model.md); four [postmortems](postmortems/)
- Prompts are versioned files ([ADR 0005](decisions/0005-versioned-prompts.md)); a prompt change is decided by a paired A/B on the eval cases, on a rule set before the run (`make ab`)

## 8. Before any real use

- Clinical, privacy and security review; a signed BAA with the LLM provider, or self-hosted models
- Validation on the hospital's own data, with fairness checks across age, language, race and payer
- Access control and access logs on the nurse screen and the timeline
- A prospective pilot with the randomized holdout, and a stopping rule
