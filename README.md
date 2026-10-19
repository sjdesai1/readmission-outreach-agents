# Readmission Risk + Outreach Agents

End-to-end simulation of a hospital medicine readmission program, fully synthetic and free:
messy physician notes parsed by an LLM, an XGBoost 30-day readmission model, and a LangGraph
multi-agent chain that reaches flagged patients through the channels they opted into and works
with them until a follow-up or telehealth visit is booked. Airflow runs it day by day on a
simulated clock; LangSmith traces and evaluates every step.

Docs: one per milestone in [docs/](docs/), interview Q&A in [docs/interview_qa.md](docs/interview_qa.md)

## Status

v1: working end to end. v2: see the roadmap below.

| # | Milestone | Status |
|---|---|---|
| 0 | Local stack: Postgres, Airflow, MLflow, Mailpit, smoke test, CI | done |
| 1 | Simulated hospital, LLM note extraction, XGBoost risk model, live scoring | done |
| 2 | MCP servers: patient data, scheduling, messaging, referrals, case store | done |
| 3 | Multi-agent system in LangGraph: 5 agents, rule nodes, guardrails, nurse handoff, memory, tracing | done |
| 4 | Conversation loop: simulated days, scripted patients, appointments, nurse queue, outcomes and holdout, re-scoring, cost controls | done |
| 5 | Evals: per turn, per conversation, guardrails, red team, outcomes, prompt A/B | |
| 6 | LLM judge and judge assessment | |
| 7 | Timeline viewer, `make demo` | |

## Quick start

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

## Roadmap (v2)

- Uplift model: a causal forest (EconML) on the randomized holdout data, to rank patients by how much outreach helps them, not only by risk
- PCP notifications: a daily digest to each patient's primary care doctor when a patient crosses a risk threshold or has an ED visit; deduplicated, LLM summary checked against the facts, measured by open rate and visit attendance
- Learning sequencer: a bandit choosing channel and timing per patient
- Cost: cheaper model routing per agent, prompt caching, batch overnight work
