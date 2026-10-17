# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org/);
each minor version is a finished milestone.

## [Unreleased]

### Added
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

### Fixed
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
