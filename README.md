# Readmission Risk + Outreach Agents

End-to-end simulation of a hospital medicine readmission program, fully synthetic and free:
messy physician notes parsed by an LLM, an XGBoost 30-day readmission model, and a LangGraph
multi-agent chain that reaches flagged patients through the channels they opted into and works
with them until a follow-up or telehealth visit is booked. Airflow runs it day by day on a
simulated clock; LangSmith traces and evaluates every step.

Docs: one per milestone in [docs/](docs/), interview Q&A in [docs/interview_qa.md](docs/interview_qa.md)

## Status

v1: working end to end. v2: cost optimization and a learning sequencer.

| # | Milestone | Status |
|---|---|---|
| 0 | Local stack: Postgres, Airflow, MLflow, Mailpit, smoke test, CI | done |
| 1 | Simulated hospital, LLM note extraction, XGBoost risk model, live scoring | done |
| 2 | MCP servers: patient data, scheduling, messaging, referrals, case store | done |
| 3 | Multi-agent system in LangGraph: 5 agents, rule nodes, guardrails, tracing | |
| 4 | Conversation loop: sequencer, scripted patients, channels, nurse queue, simulated days | |
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
