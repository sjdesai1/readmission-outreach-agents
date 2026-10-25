# Milestone 7: see what happened

Goal: anyone (a reviewer, a clinician, an interviewer) can open one patient's case and read what the system did, in order, without reading logs or SQL.

```bash
make demo         # fresh clone -> stack, world and model, 30 simulated days, summary and links
make timeline     # http://localhost:8502
```

## 1. Case timeline

An audit trail is the ordered record of every action on a case and who took it. The timeline is that record, put in plain words on one screen.

What it shows, oldest first, grouped by day since discharge:
- Discharge, the risk tier and the model's top reasons (SHAP drivers), the barriers on file
- Every message: patient, AI and nurse, with channel, delivery status and quiet-hours holds
- What the AI decided: how it read each reply (intent), referrals filed, barriers and preferences saved, language and channel changes, identity checks
- Nurse handoffs (kind, priority, interpreter) and how the nurse resolved them
- Bookings, reschedules, cancellations, no-shows, attended visits
- Re-scores after discharge (landmark days, ED visits, missed fills) with a risk chart
- The outcome: readmitted, or the case closed and why

Shortcuts open one example of each story: readmitted, urgent nurse handoff, needed an interpreter, switched language, missed a visit, attended, risk rose after discharge, filed a referral, opted out, no reply.

Why it is built this way:
- Read only, and built from the tables the system already writes (cases, events, messages, nurse tasks). No extra logging for the demo, so the screen can't disagree with the audit trail
- The wording lives in `src/readmit/viewer/timeline.py` (tested, tests/test_timeline.py); `app.py` only draws it
- Inside one agent turn everything shares the simulation clock's minute, so items in the same minute keep the audit log's order (every message and nurse task has an audit event): the order things actually happened, not a guessed one
- Non-English messages can be machine translated, cached, labeled as such (the nurse screen's rule)

Limitations:
- Holdout patients (the randomized 20% with no outreach) have no case, so no timeline
- The LLM's reasoning isn't stored, only its decisions (the intent it read, the actions taken). Full prompts and outputs are in LangSmith when tracing is on
- Synthetic patients only; a real deployment would need access control and an access log on this screen, like any chart

## 2. make demo

A demo target is one command from a fresh clone to something to click through. Without it, a reviewer needs ten steps in the right order (`up`, seven pipeline steps, `outreach-setup`, `simulate`), and one missed step means an empty screen.

What it does:
- Creates .env from the example if missing, starts the Docker stack
- Waits for Postgres and the five MCP servers to accept connections (up to 4 minutes), naming whatever is still starting
- Builds the world and the model only if live scores are missing: world, cohort, social factors, train, score, signals, re-score models. Later runs skip this (`rebuild=1` forces it)
- Resets outreach and runs 30 simulated days with the rule-based agents
- Prints cases by status, messages, bookings, nurse tasks, readmissions, up to 5 cases worth opening, and the links

```bash
make demo                 # free: rule-based agents, keyword social factors if no key
make demo llm=1           # LLM agents: at most 5 outreach turns an hour, capped by LLM_DAILY_BUDGET_USD
make demo rebuild=1 days=60
```

Without an API key, the social factors from notes come from a keyword baseline (`extract.social --rules`) instead of the LLM:
- Generic clinical phrases per factor ("lives alone", "no car", "homeless", "ETOH", "ran out of meds"), sentence by sentence
- A sentence about a relative ("Dtr lives alone nearby"), family history, or a denial ("Denies ETOH") doesn't count
- The evidence is the sentence itself, so it passes the same grounding check as the LLM's quotes
- Tested on every phrasing the simulator writes, positive and negative

Limitations:
- The keyword rules match the simulator's phrasings because they were checked against them; on real notes they would miss unseen wording and negations ("no longer homeless"), which is why the LLM extractor exists. `make extract-eval` scores whichever ran last against the answer key
- The first run takes a few minutes (world generation, training, re-score models); Docker image builds add more on a new machine
- `llm=1` runs a smaller program (5 turns an hour) so it stays inside the free tier's rate limits
