# Readmission Risk + Outreach Agents

A simulated hospital medicine readmission program, end to end, synthetic and free:
- Messy physician notes parsed by an LLM, every finding checked against the note
- An XGBoost 30-day readmission model with SHAP reasons, and re-scoring after discharge
- A LangGraph multi-agent system that reaches flagged patients by SMS, email, portal or phone, only on channels they opted into, books a follow-up or video visit, and hands anything clinical to a nurse
- Evals at every level, an LLM judge checked against people, and a randomized holdout to measure the effect

Start with the [system card](docs/system_card.md): purpose, design, results, limitations, safety controls.
Then one doc per milestone in [docs/](docs/), decisions in [docs/decisions/](docs/decisions/), interview Q&A in [docs/interview_qa.md](docs/interview_qa.md).

## Results

| | |
|---|---|
| Risk model | AUROC 0.804 vs 0.714 for LACE, the clinical standard |
| Agent single turns | 57/57 (rule-based twin 52/57) |
| Red team | 34/34 attacks stopped by the defense they target |
| Whole conversations | 21/22 personas end correctly over 8 simulated days |
| LLM judge | 45/56 planted errors, 0/15 false alarms, kappa 0.80 vs a person |
| Program effect | randomized estimate within 0.7 points of the truth; the naive one 63% too high |

## Status

v1.0.0: working end to end. v2: see the roadmap below.

| # | Milestone | Status |
|---|---|---|
| 0 | Local stack: Postgres, Airflow, MLflow, Mailpit, smoke test, CI | done |
| 1 | Simulated hospital, LLM note extraction, XGBoost risk model, live scoring | done |
| 2 | MCP servers: patient data, scheduling, messaging, referrals, case store | done |
| 3 | Multi-agent system in LangGraph: 5 agents, rule nodes, guardrails, nurse handoff, memory, tracing | done |
| 4 | Conversation loop: simulated days, scripted patients, appointments, nurse queue, outcomes and holdout, re-scoring, cost controls | done |
| 5 | Evals: per turn, per conversation, red team and threat model, program effect, prompt A/B, Grafana monitoring | done |
| 6 | LLM judge, checked against planted errors and human labels; judge scores in A/B and Grafana | done |
| 7 | Case timeline viewer, `make demo`, system card | done |

## Quick start

Just look at it (Docker and uv; no API key, no cost):
```bash
make setup && make demo     # stack, world and model (first run: a few minutes), 121 simulated days, links
make timeline               # one case's whole story at http://localhost:8502
```

Step by step:
```bash
make setup     # .env + local Python env; then add your Gemini and LangSmith keys to .env
make up        # start the stack
make smoke     # check every service
```

Then run the milestone 1 pipeline: `make pipeline`

Milestone 2: `make outreach-setup` (calendar, clock, cases), `make inspector` (call MCP tools by hand)

Milestone 3: one turn at a time, traced in LangSmith
```bash
uv run python -m readmit.agents.run C000856 --at 2026-09-10T09:00                     # first outreach
uv run python -m readmit.agents.run C000856 --reply "the 2nd one" --channel sms        # patient reply
uv run python -m readmit.agents.run --nurse-queue                                      # open nurse tasks
```
After changing `src/readmit/mcp` or `src/readmit/outreach`, restart the servers:
`docker compose restart mcp-patient-data mcp-scheduling mcp-messaging mcp-referrals mcp-cases`

Milestone 4: the program over simulated days
```bash
make outreach-setup && make simulate days=90     # rule-based agents, free; llm=1 for the real agents
make nurse                                       # nurse queue screen at http://localhost:8501
make signals && make landmark                    # post-discharge signals, re-score models
make program && make program-eval                # 20x program in its own database: outreach-signal study
```

Milestone 5: evals and monitoring
```bash
make eval-turns llm=1 && make eval-conversations llm=1 && make eval-redteam llm=1
make effect             # program effect vs the truth (20x program; db=app for the demo)
make ab                 # prompt A/B, paired (task=, a=, b=)
```
Dashboard and alerts: http://localhost:3000 · runbook: [docs/runbook.md](docs/runbook.md) · threat model: [docs/threat_model.md](docs/threat_model.md)

Milestone 6: LLM judge
```bash
make eval-judge         # grade the messages from the last LLM eval runs (agents=rule-based for the templates)
make judge-label        # label 20 judged messages yourself, blind to the judge
make judge-check        # planted errors + agreement with your labels (kappa)
make ab judge=1         # prompt A/B with judge scores
```

Milestone 7: see what happened
```bash
make timeline           # one case's whole story at http://localhost:8502 (shortcuts to interesting cases)
make demo               # everything above from a fresh clone (llm=1 for the real agents, rebuild=1, days=)
```

## Roadmap (v2)

### As real as a free simulation can be

Goal: remove every v1 shortcut a free simulation can replace, so the program runs the way a hospital would run it, only faster and with synthetic people.

| v1 shortcut | v2 |
|---|---|
| A Python loop drives time | Airflow runs the hospital's hourly and nightly jobs on a shared accelerated clock (one simulated hour per real interval, set per run) |
| Patients called inside the same process | A separate patient service; replies arrive only through an inbound webhook, the path a provider like Twilio uses |
| Patients follow fixed scripts | Patients played by an LLM with a persona, memory and realistic reply delays (a local model through Ollama, free), so they can surprise the system |
| SMS is a database row | A local SMS gateway mock: delivery receipts, delays, bounces, inbound webhooks (email already goes through real SMTP, Mailpit) |
| Nurses are a script | A nurse service working shifts, plus a person as a real nurse in the nurse screen |
| Patient data in our own tables | A FHIR server (HAPI FHIR) as the EHR, patients from Synthea, agents reading through it (revisits ADR 0001) |
| One process, one turn at a time | A queue (Redis) and workers running turns in parallel, with a lock per case |
| Model trained once | Nightly DAGs: re-scores, drift monitoring, scheduled retraining, evals and the judge on yesterday's messages |
| Failures only in tests | Injected failures: LLM outages, SMS delays and bounces, clinic cancellations, an MCP server down, a flooded nurse queue |
| No access control | Logins and roles for nurses, an access log on every chart and timeline view |

Kept simulated on purpose: the hidden risk formula and true outcomes, so every number can still be checked against the truth. The fast loop stays for evals and `make demo`; the scripted personas stay for repeatable evals.

### Product and model

- Patient messages with urgency: high-tier patients told they were flagged for extra follow-up "to help you avoid going back to the hospital" (a purpose, never a prediction or score), with a real deadline ("Dr. Test would like to see you by Mon Sep 14") and their own doctor named; a fixed opening sentence written by code, the rest by the writer; a judge criterion for urgency; an A/B of this framing against "flagged as likely to be readmitted", decided by booking rate; ADR for the choice
- Message polish from judge labeling: re-offer the times instead of "the options from our last message"; when declining a diagnosis by text, say why (privacy) and that a nurse will go over it; a booking confirmation mentions the ride arranged when the patient has no transport
- Uplift model: a causal forest (EconML) on the randomized holdout data, to rank patients by how much outreach helps them, not only by risk
- PCP notifications: a daily digest to each patient's primary care doctor when a patient crosses a risk threshold or has an ED visit; deduplicated, LLM summary checked against the facts, measured by open rate and visit attendance
- Learning sequencer: a bandit choosing channel and timing per patient
- Cost: cheaper model routing per agent, prompt caching, batch overnight work
- Self-hosted inference: vLLM serving an open 7-8B model; per-agent routing between it and Gemini decided by the evals; structured output, prefix caching, load tests, serving metrics on the Grafana dashboard
- Languages beyond English and Spanish: templates, date checks and evals per language (the preference, Spanish STOP words, interpreter handoff and translated nurse view are done: [ADR 0010](docs/decisions/0010-language-is-a-stored-fact.md))
