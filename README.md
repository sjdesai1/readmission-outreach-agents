# Readmission Risk + Outreach Agents

End-to-end simulation of a hospital medicine readmission program, fully synthetic and free:
messy physician notes parsed by an LLM, an XGBoost 30-day readmission model, and a LangGraph
multi-agent chain that reaches flagged patients through the channels they opted into and works
with them until a follow-up or telehealth visit is booked. Airflow runs it day by day on a
simulated clock; LangSmith traces and evaluates every step.

Design: [docs/00_design.md](docs/00_design.md)

## Status

| # | Milestone | Status |
|---|---|---|
| 0 | Local stack: Postgres, Airflow, MLflow, HAPI FHIR, Mailpit, smoke test, CI | in progress |
| 1 | Synthetic world: patients, stays, messy notes, truth, FHIR, ADT events | |
| 2 | Ingest DAG, dbt staging, data tests, dead letter | |
| 3 | Note extraction, validators, extraction eval | |
| 4 | Features, baselines, XGBoost, calibration, SHAP, MLflow | |
| 5 | Scoring DAG, tiers, holdout, worklist | |
| 6 | MCP servers | |
| 7 | LangGraph chain of action, guardrails, tracing | |
| 8 | Patient simulator, outreach DAG, channels, portal, nurse queue, timeline viewer | |
| 9 | Evals, LLM judge, judge assessment, eval report, CI gates | |
| 10 | Shadow mode, retrain, monitoring, fairness, `make demo` | |

## Quick start

```bash
make setup     # .env + local Python env; then add your Gemini and LangSmith keys to .env
make up        # start the stack
make smoke     # check every service
```

Details: [docs/01_m0_local_stack.md](docs/01_m0_local_stack.md)
