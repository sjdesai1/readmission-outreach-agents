# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org/);
each minor version is a finished milestone.

## [Unreleased]

## [0.6.0] - Milestone 6

### Added
- docs/08_m6_judge.md: results for every M6 step; ADR 0011 (the judge grades wording, code checks promises and
  formality); interview Q&A for M6; README: milestone 6 done, commands, the labeling fixes on the roadmap
- Final numbers: judge v2 passes 95/96 LLM messages and 88/96 template messages on every criterion. Judge check:
  45/56 planted errors caught (wrong time, medical advice, pressure, jargon 8/8 each; broken English or tú 4/8;
  next step removed 4/8; ignores the patient 5/8), 0/15 false alarms; agreement with 20 human labels 18/20,
  kappa 0.80, lenient twice (unbacked "a nurse will follow up"), never stricter. A/B rerun on 87 cases: v10 and
  v11 tie on every metric, so milestone 5's "2.9 words shorter" did not replicate; v11 stays
- Milestone 6 step 3: judge results on the dashboard: Grafana reads the eval database (new data source "evals")
  for a "Message quality" row: the latest LLM run's share of messages passing every criterion, and every judge
  run and judge check (planted errors caught, false alarms, kappa). Each run writes a row to ops.judge_runs.
  `make ab judge=1` adds "judge ok" (the judge passes the message on every criterion) as a paired secondary metric

- Milestone 6 step 2: checking the judge, on data it wasn't tuned on. `make judge-check`: planted errors, one flaw
  per message aimed at one criterion (wrong time, medical advice, pressure, jargon, informal or broken language,
  next step removed, a reply that ignores the patient), up to 8 per flaw, plus untouched originals for false
  alarms. `make judge-label`: label 20 judged messages in the terminal, blind to the judge's verdict; saved with
  their context in evals/judge_labels.jsonl. Agreement on pass/fail with Cohen's kappa, lenient and strict cases,
  per criterion. Saved to data/reports/judge_check.json and MLflow
- Milestone 6 step 1: LLM judge (`make eval-judge`, `agents=rule-based`, `limit=n`). A second model
  (LLM_JUDGE_MODEL, not the writer's) grades every message patients got in the last turn, red-team and
  conversation eval runs on seven yes / no / n/a criteria, each with a reason written first: answers the patient,
  clear next step, warm and respectful tone, plain language, no medical advice, language quality (Spanish:
  natural, "usted"), consistent with the facts. Identical messages in the same context are graded once. Reports
  pass rate per criterion, messages passing all, by source, the failures with reasons, and the length of passing
  vs failing messages (a check for verbosity bias). Saved to data/reports/judge_<agents>.json, logged to MLflow.
  Turn eval facts now include the times offered, the visit booked and the messages before the turn;
  conversation reports include transcripts
- First run: LLM agents 86/96 messages pass every criterion, rule-based templates 69/96; the difference is almost
  all "answers the patient" (89% vs 67%). Passing and failing messages are about the same length (43 vs 40 words)
- Judge rubric v2 after reading the judge's own failures: it now sees the messages before a turn ("the second
  one" refers to an offer it couldn't see), declining what the system must never do (a diagnosis by text, its
  instructions) counts as responding, and code, not the judge, marks "consistent with the facts" n/a when no
  facts are given and "answers the patient" n/a when the patient hasn't written since our last message
- LLM_JUDGE_MODEL defaults to gemini-3.1-flash-lite: gemini-3.5-flash ran out of free daily quota after 17 calls
- Conversation evals: failures list what the agents decided (intents, preferences saved, barriers, bookings)
- Language preference (ADR 0010). An explicit request ("text me in Spanish", "ESPAÑOL", "en inglés por favor")
  is caught by the input guard and stored in patient memory through a new `set_language` tool (audited,
  overrides the record); the reply confirms the switch and offers the times again in the new language. A reply
  written in the other language gets its normal answer plus one question, asked once per patient. A request for,
  or a reply in, any other language goes to a nurse task flagged with the interpreter language; the patient gets
  an acknowledgement in the language on file and the nurse keeps the case
- agents/language.py: request patterns, detection by script and stopwords (no decision under three words)
- Spanish opt-out words in the STOP list (PARAR, BASTA, ALTO, NO MÁS, CANCELAR, DETENER, BAJA), accents ignored
- Interpreter v6: `language_change` intent and `new_language`, a second net behind the rules
- Nurse screen: machine translation under messages not in English (prompt translate_v1, cached, labeled as
  machine translation), interpreter flag in the queue and the task header
- Evals: 12 turn cases (now 53), 3 red-team cases (now 34: Spanish STOP, injection inside a language switch,
  a diagnosis request in French), 2 conversation personas (switches_language, needs_interpreter; now 22 per seed)
- Schema: outreach.nurse_tasks.interpreter

### Changed
- Planted language errors v2: an error needs at least 2 swapped words (v1 let single-word changes through, so a
  few "errors" were barely there), and more usted -> tú forms (verbs with "le", "se comunicará con usted",
  "llame", "tiene", "necesita")
- Conversation evals: the abusive persona's goal accepts an opt-out as well as a nurse review ("stop bothering me,
  you people are useless" is both; the turn eval already accepted either)

### Fixed
- A patient whose picked time was gone could be offered times against their standing preference (busy Maria,
  "I work mornings", got Mon 9:30 AM). The reply "the first one" only names that time, so its details (the gone
  slot's day) are no longer used as a new request; and when nothing fits a request, the standing preferences are
  tried before any open time. Regression test in test_agent_graph
- Scripted patients answer up to 3 new offers after their script, not 2: Jose (med_question) used one after the
  nurse call and one after his pick was taken, so he couldn't rebook after a missed visit
- Scripted patients ignore the once-only language question (its "?" made a booking confirmation look like an offer)
- A booked patient who replied (a question, a language change) was set back to awaiting_reply, so outreach could
  offer new times on top of the booked visit; booked cases now stay booked

## [0.5.0] - Milestone 5

### Added
- docs/07_m5_evals.md: results and findings for every M5 step; interview Q&A for M5; README: milestone 5 done,
  commands, v2 roadmap adds self-hosted inference (vLLM) and language preference; runbook: recreate a container
  whose mounted files changed
- Milestone 5 step 6: monitoring. Grafana in docker compose (http://localhost:3000, no login locally) with the
  data source, the "Outreach operations" dashboard and the alert rules provisioned from docker/grafana. Panels:
  LLM spend vs the cap, calls and failures per hour, cost and p95 latency by task, open and overdue nurse tasks,
  tasks by kind, cases the AI keeps failing on, case status, funnel, messages by channel, bounces and opt-outs,
  re-scores by trigger. Alerts, emailed to Mailpit: spend over 80% of the daily cap; a case with 3+ review tasks.
  docs/runbook.md: each alert, plus nurse backlog, MCP servers down, bounces. `make smoke` checks Grafana too
- Milestone 5 step 5: prompt A/B test (`make ab`, any `task`, `a`, `b`). Both versions answer the same turn and
  red-team cases; primary metric pass rate (exact McNemar on disagreeing cases), secondary words per message,
  reading grade, rewrites and reviewer advisories (paired bootstrap CI, Wilcoxon). Candidate writer v11: brief by
  rule (SMS under 220 characters, email under 60 words, no filler). Turn eval facts now include the draft, rewrites
  and advisories
- A/B result, write_message v10 vs v11, 72 paired cases: pass rate 99% vs 100% (no case lost, McNemar p = 1.0);
  words per message 41.6 -> 38.7 (95% CI -5.0 to -1.0, p = 0.008); reading grade unchanged (4.3 vs 4.1);
  rewrites 15% -> 6% (CI -24% to +6%, not significant). v11 is now the live writer
- Milestone 5 step 4: program effect (`make effect`, `db=app` for the demo). Outcomes re-rolled 1000 times with
  the conversations held as they happened; three estimators graded against the simulation's truth: randomized
  (holdout minus outreach), adjusted for discharge risk (robust standard errors), naive (attended vs not).
  Bias, spread, CI width, coverage, power, the split's baseline imbalance, and patients per arm needed
  First run, 20x program (1,317 patients): truth 5.0 points; randomized 5.7 (coverage 97%, power 46%); adjusted
  6.3 (coverage 95%, power 60%); naive 14.3 against a true attending effect of 8.8 (coverage 48%). 1,234 patients
  per arm needed for 80% power: the 20% holdout (286) is the bottleneck. Refuses to run with under 10 per arm
- Milestone 5 step 3: red team (`make eval-redteam`, `llm=1`). 31 attacks in evals/redteam.jsonl: prompt
  injection (English, Spanish, role play, admin mode), health information and contact leaks, impersonation,
  phone identity bypass, excessive agency (book every slot, someone else's slot, invented or past times), prompt
  leakage, medical advice requests, hidden emergencies, STOP variants and a false STOP, threats, very long
  messages. Each tagged with the defense expected to stop it; the report adds results by defense.
  `status_not` check. docs/threat_model.md: assets, actors, OWASP LLM Top 10 mapping, residual risks
- First red-team run: LLM agents 31/31; rule-based 25/31. The rule-based agents are also the over-budget fallback,
  so their gaps matter. Fixed:
  - emergency patterns: "me falta el aire", "me duele el pecho", "my chest hurts", fainting in Spanish. Rule-based,
    this booked a visit for a patient short of breath
  - a yes with its own day or time ("book me yesterday at 3 AM") is not a yes to the offered options: offer, don't
    book; a numbered pick ("the second one") is a yes
  - medication questions ("should I take two?", "pill", "is it normal") and threats ("you'll regret it") are read
- Milestone 5 step 2: conversation evals (`make eval-conversations`, `llm=1`, `seeds=n`). Every persona runs a
  whole conversation over 8 simulated days with nurses working the queue, 20 conversations per seed. Scored on
  the persona's goal (booked, afternoon for busy, ride or video for no ride, nurse for questions and symptoms, 3
  attempts for silent, opted out, AI stops after abuse) and on rules for every conversation (privacy, Spanish for
  Jose, quiet hours, nothing after STOP). Metrics: AI messages, messages before booking, hours to booking, nurse
  tasks. Report to data/reports and MLflow (experiment `conversation-evals`)
- First conversation eval run: rule-based 16/20, LLM 2/20. Fixes:
  - scripted patients answered only "?", "reply" or "responda"; LLM messages say "let us know which works", so
    every patient looked silent. They now answer numbered options and other ways of asking
  - failure report shows nurse task reasons (counted) and the last messages; `only=<persona>` filter
  - quiet hours rule allows the STOP confirmation at any hour (it must go out right away)
  - writer v10: a rebooking message doesn't repeat the missed visit's time; says plainly when a picked time is gone
  - reviewer v4: fails only for something wrong or unsupported, not for an omitted detail; rebooking after a
    missed visit and services from the talking points are expected
- Circuit breaker: from the third review task on a case, the task tells the nurse to handle it directly, and the
  simulated nurses take it over instead of handing it back
- Second conversation eval run: rule-based 18/20, LLM 8/20. Fixes (ADR 0009):
  - offers are stored when sent (outreach.offers; scheduling tools `record_offers`, `get_offered_slots`), and a
    reply picking a numbered option books exactly that slot. If it's gone, the patient is told and offered new
    times: never a different time booked for them
  - the reviewer's safety checks always block; polish-only reasons after one rewrite are sent as advisory, so a
    nitpick ("option 3 doesn't name the doctor") no longer stops outreach
- Third conversation eval run: LLM 15/20 (from 8), turn evals 41/41. Fixes:
  - offered times start 18 hours out (booking still allows 2): a same-day time was often gone by the time the
    patient replied, hours later
  - scripted patients recognize offers written as "1:", "1 -" or a single time with a question
  - writer v10: Spanish always uses the formal "usted"
- Milestone 5 step 1: per-turn evals (`make eval-turns`, `llm=1` for the LLM agents). 40 cases in
  evals/turns.jsonl across booking, scheduling, questions, clinical questions, symptoms, emergencies, barriers,
  opt-out, privacy, abuse, prompt injection, identity, handoff, memory and reminders. Checks intent, route,
  action, status, nurse task, booking, referral, memory and message content, plus a privacy check on every case.
  Report by category and by check, saved to data/reports and MLflow (experiment `turn-evals`)
- The two-patient fixture world moved to src/readmit/evals/fixture.py, shared by the tests and the evals
- First eval run: rule-based 27/40, LLM 35/40, privacy 40/40 for both. Fixes from the failures:
  - writer v9: on turns with no new slots, never repeat times from earlier messages (they may be taken; the
    output check rejected them twice and the turn went to a nurse)
  - reviewer v3: a good answer isn't failed for not pushing a booking; cost, transport and housing problems are
    handled with referrals and a kind reply, never escalated
  - rule-based writer in Spanish too (it is also the over-budget fallback): Jose was getting English
  - eval case fixed: "short of breath" inside a yes is an emergency (911, no booking), which is right; a milder
    hidden symptom case added
  - failure report shows why a turn escalated
  - Spanish patients: the LLM writer and reviewer get slot times and visit types in Spanish (the reviewer
    rejected English dates in a Spanish message while the writer was told to copy them exactly)

### Fixed
- Spanish messages offering a Tuesday slot never sent: "mar 15 sep" (martes) was read as March 15, failed the
  date check twice and went to a nurse, every hour. Spanish short dates are now checked properly (they weren't
  checked at all). Postmortem: docs/postmortems/2026-10-19-tuesday-is-march.md
- The one-message-a-day rule (0.4.0) also counted bounced messages, so after a bounce no other channel was tried
  until the next day and a patient with no working channel waited a day for a nurse. Bounces no longer count

## [0.4.0] - Milestone 4

### Added
- Milestone 4 step 8: cost controls. Daily LLM spend cap (`LLM_DAILY_BUDGET_USD`, default $1): over it, new calls
  are refused without reaching the API, cached answers still return, and turns run on the rule-based agents; the
  reply interpreter falls back to rules instead of escalating. At most one AI message per case per day starts
  from outreach. `make simulate llm=1` prints calls, cache hits, failures, tokens, cost and budget left
- docs/06_m4_simulation.md, ADR 0008 (spend cap), README: milestone 4 done, run commands, v2 roadmap (uplift
  model, PCP notifications, learning sequencer, cost)
- Milestone 4 step 1: simulated days (`make simulate days=14`): hourly clock, held messages delivered, outreach
  turns in daytime hours only
- Outreach cadence: day 0, follow-up at +48h, a phone call at +96h if calls are allowed, then closed as
  unreachable; writer v7 says it's following up
- Booked and nurse-held cases are no longer outreach-due
- Milestone 4 step 2: scripted patients. Each case's hidden persona (engaged, busy, silent, medical question,
  symptomatic, opt out, no ride, injection attempt, abusive, Spanish speaker) replies with a fixed script and
  seeded timing; phone patients give their year of birth when asked; the run ends with outcomes per persona
- Rule-based interpreter recognizes symptoms, medical questions, barriers, requests for a nurse and Spanish yes
- Milestone 4 step 3: appointment lifecycle. Day-before reminders (fixed template: time, visit type, doctor;
  one per visit); visits marked attended (case completed) or no-show (seeded per persona); the clinic cancels
  some visits. No-shows and clinic cancellations restart outreach with a fresh cadence and a rebooking offer
  (writer v8: no blame after a no-show, an apology after a clinic cancellation)
- Milestone 4 step 4: real-world events. Bounced emails and wrong numbers (seeded per patient) mark the
  channel undeliverable, a wrong number blocks texts and calls, and the next attempt goes out on another channel
  (or to a nurse if none is left). A readmission stops outreach at once and frees any booked visit. Barriers
  patients mention ("no ride") are recorded with the model's barrier names; the planner sees them and the
  rule-based planner files a transport referral
- Referrals are idempotent per case and type, not per turn (no duplicate ride referrals)
- Milestone 4 step 5: 30-day outcomes with a real outreach effect. Each flagged live stay gets a true risk with
  usual care, a heterogeneous reduction from a completed visit, and one seeded draw that defines both potential
  outcomes. The scoring holdout (20%, no outreach) makes outreach vs holdout a randomized comparison; the run
  reports it next to the biased attended-vs-not comparison and the true effect (truth.outcome_draws,
  truth.outcome_results). Replaces the step 4 stand-in that only saw later admissions
- Milestone 4 step 6: nurse queue screen (`make nurse`, Streamlit): queue in work order with lateness, the
  patient, risk and barriers, the shared thread (patient, AI and nurse messages), reply and resolve / hand back.
  Messages record their author; agents see nurse messages labeled. Simulated nurses work the queue (urgent any
  hour, routine on weekday shifts) and hand cases back, so escalated patients can still get booked. A hand-back
  restarts the cadence, and nurse notes don't count as outreach attempts
- Milestone 4 step 7a: landmark re-scores at day 7, 14 and 21. The world now generates post-discharge signals
  (pharmacy fills, ED visits, kept follow-ups) from the hidden truth (`make signals`). Landmark models train on
  patients still home at each landmark and are compared with the same patients without the signals, by
  5-fold cross-validation grouped by patient (`make landmark`, logged to MLflow). The simulation re-scores live cases at each landmark and reorders
  outreach. Outreach behavior is left for after a cold start (step 7b)
- Milestone 4 step 7a-2: re-scores update the discharge risk instead of refitting. The landmark model is now a
  logistic regression on the discharge risk (log-odds prior) plus the signals: 5 parameters, saved as JSON with
  odds ratios. Event-triggered re-scores: an ED visit or no prescription fill by day 3 re-scores the case at once
  and creates a nurse callback (urgent for an ED visit, routine for a missed fill) when the risk rises 5 points or
  more or reaches 50%. Days 7, 14 and 21 stay as a safety net. `store.rescore` records the trigger. Fill signals
  are yes/no (`rx_missing`, `rx_late`) so they mean the same on any day, and a day-3 model serves the missed-fill
  re-score. Cases whose outreach ended are still re-scored until a readmission, and an ED visit still gets a
  callback (it doesn't reopen outreach); an event before the case opens is handled when it opens.
  The simulation prints re-scores by trigger with the average risk change and the callbacks created

- Milestone 4 step 7b: outreach signals after the cold start. `make program` builds a 20x hospital in its own
  database (`program`, the demo is untouched), scores it with the champion, and runs 4 months of outreach with
  the rule-based agents and in-process MCP servers. Note extraction is simulated at the real extractor's
  measured error rates (no LLM cost). `make program-eval` compares the day 7 / 14 re-score with and without
  outreach signals (replied, silent, booked, no-show, barrier, unreachable) by grouped CV, with odds ratios and
  a learning curve over program size. Result on the main world's rules: no lift (day 7 -0.015, day 14 +0.003,
  CIs include zero), because personas there are independent of risk: a simulation only reveals what it encodes.
  `make program control=1` is a positive control where disengaged patients carry extra risk (world option
  `engagement_risk`, off in the main world, which is unchanged). Program runs turn LangSmith tracing off
  The evaluation's main test is pre-specified (replied, silent only), reports log-loss gain next to AUROC lift
  (both with bootstrap CIs), and in the positive control adds an oracle row (the true engagement risk) as the
  ceiling for any engagement signal
- docs/05_m4_rescoring.md: re-scoring design, results, the cold-start study and decisions; interview Q&A for M4
- `outreach.setup.calendar` takes how many slots other patients hold, so a bigger program has room

### Fixed
- The step 7a landmark models (full XGBoost refit) overfit the few readmissions left after each landmark and
  lowered AUROC (day 14: -0.022, CI excluding zero); replaced by the prior update above
- Removed a stray `.agents/` editor folder committed by accident; `.agents/` is now gitignored
- `make outreach-setup` left nurse tasks and patient memory from earlier runs; a replayed idempotency key then
  returned a stale nurse task and the case was never paused. Setup now empties every outreach table
- Flaky "too many clients" test error: the test fixture now closes its database connections after each test

## [0.3.0] - Milestone 3

### Added
- Milestone 3: LangGraph turn graph with rule nodes and an MCP gateway with per-role tool access
- Reply interpreter, care planner and message writer agents with versioned prompts and Pydantic schemas
- Scheduler agent: tool-calling loop over the scheduling MCP server with runtime tool discovery; code injects
  case and idempotency keys and blocks invented or ruled-out slots; rule-based fallback
- Guardrails: STOP keywords, emergencies, family-member replies, injection flags, invented-time and tone checks,
  no diagnosis outside the patient portal, booking guard that never books a time the patient ruled out
- Privacy: the LLM never sees the patient's name; contacts masked in prompts and traces
- Server rules: quiet hours, provider matching (follow-up deadline, language, own doctor), channel changes,
  identity check by year of birth on the phone
- Safety reviewer agent: a second model reads every draft after the rule checks; medical advice goes to a nurse
- Nurse handoff: nurse tasks with honest due times from nurse capacity, fixed acknowledgements in the patient's
  language, the AI pauses on the case until a nurse hands it back
- Patient memory: standing preferences ("I work mornings") are saved and used in later turns and stays
- Writer v4: "video visit" / "clinic visit" labels; run.py prints scheduler steps, review, nurse task and queue
- Repo hygiene: pre-commit (ruff, gitleaks), PR and issue templates, license, coverage, type check and
  dependency audit in CI, Dependabot

### Docs
- Architecture decision records (docs/decisions) and postmortems (docs/postmortems)
- Milestone 3 doc (docs/04_m3_agents.md) and interview Q&A

### Fixed
- Reviewer flagged real doctor names as invented (it wasn't given providers or the conversation); review v2
- Interpreter re-saved preferences from earlier messages as new ones; interpret v4, and code skips unchanged keys
- Remembered "afternoons" was dropped when the patient named a day; days and time of day now combine
- Scheduler offered only video when the patient had no preference; schedule_step v2 offers a mix
- "Do you have clinic visits?" was read as a question: the writer promised visits with no slots behind it;
  interpret v5, writer v5, and code routes questions with scheduling details to the scheduler
- Reviewer escalated fixable drafts straight to a nurse; only medical advice skips the rewrite now
- Booking confirmations asked "does this time still work?" (phone style forced a closing question); writer v6
- Confirmations couldn't name the doctor; the booked slot's doctor is now passed to the writer and reviewer
- Nurse hand-back put booked patients back to "awaiting reply"; it now returns them to "booked"
- Nurse task reason now includes the patient's question, so the queue can be triaged at a glance
- Emergency nurse tasks are due immediately and listed first in the queue
- Safety: a case paused for a nurse skipped every inbound turn, so "chest pain" got no 911 message;
  paused cases now still run the input guard (emergencies and STOP), and other replies are flagged for the nurse
- Writer phrasing "an in the clinic visit"
- Agents booked a time the patient had ruled out ("can't do noon" booked noon)
- Actions were possible on a case before it opened (booking dated before discharge)

## [0.2.0] - Milestone 2

### Added
- Outreach system of record with business rules (consent, atomic booking, idempotency, audit)
- Five MCP servers: patient data, scheduling, messaging, referrals, cases

## [0.1.0] - Milestones 0 and 1

### Added
- Local stack: Postgres, Airflow, MLflow, Mailpit; smoke tests; CI
- Simulated hospital with messy notes, LLM extraction of social factors, XGBoost risk model, live scoring
