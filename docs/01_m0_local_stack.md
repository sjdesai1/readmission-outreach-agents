# Milestone 0: Local stack

Goal: every service the project needs runs on the laptop with one command, and a smoke test proves each one works, from the laptop and from inside Airflow.

## What runs, and why

| Service | What it is | Why this project needs it | Open it |
|---|---|---|---|
| Postgres | Relational database | One server, three databases: `airflow` (Airflow's own bookkeeping), `mlflow` (experiment records), `app` (patients, scores, cases, messages) | `localhost:5432` |
| Airflow | Workflow orchestrator | Runs the DAGs: ingest, extract, score, outreach ticks, retrain, monitoring, evals. Each run = one simulated day | http://localhost:8080 |
| MLflow | Experiment tracking + model registry | Logs every training run (params, metrics, model files); champion / challenger | http://localhost:5000 |
| HAPI FHIR | Real FHIR R4 server | The simulated hospital EHR. Patients, encounters, notes, slots and appointments live here, in the standard healthcare format | http://localhost:8090 |
| Mailpit | Fake email server with a web inbox | Catches every email the agents send, so they can be read | http://localhost:8025 |
| LangSmith | Hosted tracing + evals (cloud, free tier) | Shows every LLM call and agent step; runs eval experiments | https://smith.langchain.com |
| Gemini | LLM (cloud, free tier) | Note extraction, agents, judge. Reached through LiteLLM | none |

## Key ideas

### Containers and Docker Compose
- A container = an app plus everything it needs, isolated, same on every machine
- `docker-compose.yml` = the list of containers, how they connect, their ports and storage
- `docker compose up` starts them all in the right order

### Service names vs localhost
- Inside Docker, containers reach each other by service name: `postgres`, `hapi`, `mlflow`, `mailpit`
- From the laptop, the same services are at `localhost:<port>`
- `.env` holds the laptop addresses; `docker-compose.yml` overrides them for the Airflow container
- Same code, different addresses, no code change. `src/readmit/config.py` reads whichever is set

### Volumes
- Named volumes (`pgdata`, `mlflow_artifacts`, `airflow_logs`) keep data when containers stop
- `docker compose down` keeps them; `docker compose down -v` deletes them (clean reset)

### Healthchecks and start order
- Postgres has a healthcheck (`pg_isready`)
- Airflow and MLflow wait until Postgres is healthy before starting

### Secrets
- API keys live only in `.env`, which is in `.gitignore`
- `.env.example` documents every variable with blank secrets; that file is committed
- A unit test fails if the code reads a variable missing from `.env.example`

### Airflow runs project code in its own virtual environment
- Airflow pins its own library versions; XGBoost, LangGraph, LiteLLM would conflict
- The Airflow image gets a second Python environment, `/opt/app-venv`, with only the project's libraries
- Tasks call `/opt/app-venv/bin/python -m readmit.<module>`
- Airflow orchestrates, project code computes. Production version of the same idea: each task in its own container (KubernetesPodOperator)

### Airflow standalone
- One container runs the API server, scheduler, DAG processor and triggerer
- Fine for a laptop; production runs each as its own service, often managed (Astronomer, MWAA, Composer)

### MLflow: two stores
- Backend store (Postgres): run records, params, metrics, model registry
- Artifact store (volume): model files, plots, reports

### One source of truth for dependencies
- `requirements.txt` feeds both the Airflow task venv and the laptop environment (`pyproject.toml` reads it)
- It grows one milestone at a time; only what's used so far

### Smoke test
- `src/readmit/smoke.py` checks each service independently and prints PASS / FAIL / SKIP
- FHIR: write, read and delete a throwaway Patient
- MLflow: log a run with an artifact
- Mailpit: send an email, find it in the inbox
- Gemini: list the models the key can use, then one call through LiteLLM with fallback
- LangSmith: that call is traced, so a trace appears in the project
- Same module runs from the laptop and inside Airflow (`m0_smoke` DAG), proving both paths

## Files

```
docker-compose.yml          the stack
docker/postgres/init.sql    creates the airflow, mlflow, app databases
docker/airflow/Dockerfile   Airflow + separate project venv
docker/mlflow/Dockerfile    MLflow server + Postgres driver
requirements.txt            project dependencies (single source)
pyproject.toml              package config, dev tools, ruff, pytest
.env.example                every setting, blank secrets
src/readmit/config.py       settings from environment variables
src/readmit/smoke.py        the smoke test
dags/m0_smoke.py            the smoke test as an Airflow DAG
tests/test_config.py        unit tests, no services needed
.github/workflows/ci.yml    lint, tests, compose validation on every push
Makefile                    shortcuts
```

## Run it

One-time installs: Docker Desktop (give it 8 GB+ memory in Settings > Resources), uv, git, make (on a Mac it comes with Xcode command line tools: `xcode-select --install`).

```bash
cd readmission-outreach-agents
make setup          # creates .env from .env.example, builds the local Python env
# edit .env: paste GEMINI_API_KEY and LANGSMITH_API_KEY
make up             # first build takes several minutes
make ps             # all services should be "running"; HAPI takes 1-2 min to finish starting
make smoke          # every check from the laptop
make smoke-airflow  # every check from inside Airflow
make test           # unit tests
```

Without make, each target is the command written next to it in the `Makefile`.

## Done when

- [ ] `make ps` shows postgres, airflow, mlflow, hapi, mailpit running
- [ ] `make smoke`: 8/8 PASS
- [ ] `make smoke-airflow`: task succeeds, same 8/8 in its log
- [ ] Airflow UI shows the `m0_smoke` DAG
- [ ] MLflow UI shows experiment `m0-smoke` with runs and a `smoke.txt` artifact
- [ ] Mailpit inbox shows the smoke emails
- [ ] LangSmith project `readmission-outreach` shows `m0_smoke_llm_call` traces
- [ ] `make test` passes
- [ ] Gemini model names in `.env` match what the smoke test listed

## If something fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `manifest unknown` / image not found | An image version in `.env` doesn't exist | Change `AIRFLOW_VERSION`, `HAPI_IMAGE` or `MAILPIT_IMAGE` to a tag listed on Docker Hub, `make up` again |
| `port is already allocated` | Something else uses 5432, 8080, 5000, 8090, 8025 or 1025 | Stop the other app, or change the left side of the port mapping |
| FHIR check fails right after `make up` | HAPI still starting | Wait 1-2 minutes, rerun `make smoke` |
| Containers restarting or very slow | Not enough Docker memory | Raise it in Docker Desktop settings |
| Gemini call fails, models listed | Model name in `.env` retired or misspelled | Pick a name from the smoke test's list |
| Airflow can't find `readmit` | Project not mounted | Run `make up` from the repo folder |
