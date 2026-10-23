# Shortcuts. Each target is just the docker / python command written next to it.
.PHONY: setup up down reset ps logs smoke smoke-airflow test lint check format typecheck audit cov upgrade \
        world cohort extract extract-eval train score signals landmark pipeline pipeline-airflow \
        outreach-setup mcp-logs inspector turn simulate nurse program program-eval eval-turns eval-conversations eval-redteam effect ab

setup:          ## one-time: create .env, the local Python environment and the git hooks
	@test -f .env || cp .env.example .env
	uv sync --extra dev
	uv run pre-commit install

up:             ## build and start the whole stack
	docker compose up -d --build --remove-orphans

down:           ## stop the stack, keep data
	docker compose down

reset:          ## stop the stack and DELETE all data
	docker compose down -v

ps:             ## show service status
	docker compose ps

logs:           ## follow logs (make logs s=airflow for one service)
	docker compose logs -f $(s)

smoke:          ## check every service from your laptop
	uv run python -m readmit.smoke

smoke-airflow:  ## check every service from inside Airflow, through a DAG run
	docker compose exec airflow airflow dags test m0_smoke

test:           ## unit tests
	uv run pytest -q

lint:
	uv run ruff check .

check:          ## run before every push: lint + format + tests (CI runs the same)
	uv run ruff check . && uv run ruff format --check . && uv run pytest -q

format:         ## auto-format the code
	uv run ruff format . && uv run ruff check --fix .

cov:            ## tests with a coverage report
	uv run pytest -q --cov --cov-report=term-missing:skip-covered

typecheck:      ## static type check (report only for now)
	uv run pyright

audit:          ## known vulnerabilities in the locked dependencies
	uv export --frozen --no-hashes --no-emit-project > /tmp/readmit-req.txt && \
	uvx pip-audit -r /tmp/readmit-req.txt --no-deps --disable-pip

upgrade:        ## upgrade Python dependencies within their pins, then review the uv.lock diff in a PR
	uv lock --upgrade && uv sync --extra dev

# ---- milestone 1 pipeline, step by step (or all at once with `make pipeline`) ----
world:          ## generate the simulated hospital into Postgres
	uv run python -m readmit.sim.generate

cohort:         ## labels, exclusions, time split, prediction-time note cutoff
	uv run python -m readmit.data.cohort

extract:        ## LLM extraction of social factors from notes (cached: reruns are cheap)
	uv run python -m readmit.extract.social

extract-eval:   ## score extraction against the answer key, log to MLflow
	uv run python -m readmit.extract.evaluate

train:          ## train and compare models, register the champion in MLflow
	uv run python -m readmit.model.train

score:          ## score live discharges with the champion
	uv run python -m readmit.model.score

signals:        ## post-discharge signals for past stays (pharmacy fills, ED visits, kept follow-ups)
	uv run python -m readmit.sim.signals

landmark:       ## day 7 / 14 / 21 re-score models: train, compare with and without the new signals
	uv run python -m readmit.model.landmark

pipeline: world cohort extract extract-eval train score signals landmark   ## everything, in order

pipeline-airflow:  ## the same pipeline, run by Airflow
	docker compose exec airflow airflow dags test m1_build_world

# ---- milestone 2: outreach system of record + MCP servers ----
outreach-setup: ## reset outreach tables, clinic calendar, sim clock; open a case per flagged discharge
	uv run python -m readmit.outreach.setup

mcp-logs:       ## follow the MCP servers' logs
	docker compose logs -f mcp-patient-data mcp-scheduling mcp-messaging mcp-referrals mcp-cases

inspector:      ## open MCP Inspector (browser UI) to call any server's tools by hand
	npx -y @modelcontextprotocol/inspector

# ---- milestone 3: multi-agent graph ----
turn:           ## one agent turn by hand: make turn c=C000856 at=2026-09-10T09:00   (add reply="yes" ch=sms, stub=1)
	uv run python -m readmit.agents.run $(c) $(if $(at),--at $(at)) $(if $(reply),--reply "$(reply)" --channel $(or $(ch),sms)) $(if $(stub),--stub)

simulate:       ## run outreach over simulated days: make simulate days=14 (llm=1 for the real agents, cases=5 to cap)
	uv run python -m readmit.sim.days --days $(or $(days),14) $(if $(llm),--llm) $(if $(cases),--max-cases $(cases))

nurse:          ## nurse queue screen at http://localhost:8501 (first time: uv sync --extra dev --extra ui)
	uv run --extra ui streamlit run src/readmit/nurse/app.py

# ---- milestone 4 step 7b: a bigger program in its own database (`program`), for outreach signals ----
program:        ## build a 20x program world and run 4 months of outreach (no LLM, no tracing). control=1: positive control
	APP_DB_NAME=program LANGSMITH_TRACING=false uv run python -m readmit.sim.program build $(if $(patients),--patients $(patients)) $(if $(control),--control)
	APP_DB_NAME=program LANGSMITH_TRACING=false uv run python -m readmit.sim.program run $(if $(days),--days $(days))

program-eval:   ## what outreach signals add to the day 7 / 14 re-score, with a learning curve
	APP_DB_NAME=program LANGSMITH_TRACING=false uv run python -m readmit.model.outreach_signals

# ---- milestone 5: evals ----
eval-turns:     ## per-turn evals on evals/turns.jsonl (llm=1 for the LLM agents, only=<text> to filter)
	LANGSMITH_TRACING=false uv run python -m readmit.evals.turns $(if $(llm),--llm) $(if $(only),--only $(only))

eval-conversations: ## whole conversations per persona over 8 simulated days (llm=1, seeds=3)
	LANGSMITH_TRACING=false uv run python -m readmit.evals.conversations $(if $(llm),--llm) $(if $(seeds),--seeds $(seeds)) $(if $(only),--only $(only))

eval-redteam:   ## attacks from evals/redteam.jsonl, each mapped to the defense that should stop it (llm=1)
	LANGSMITH_TRACING=false uv run python -m readmit.evals.turns --dataset redteam $(if $(llm),--llm) $(if $(only),--only $(only))

effect:         ## program effect estimators vs the truth, over 1000 outcome draws (db=app for the demo program)
	APP_DB_NAME=$(or $(db),program) LANGSMITH_TRACING=false uv run python -m readmit.evals.effect $(if $(draws),--draws $(draws))

ab:             ## prompt A/B on turn + red-team cases, paired (task=write_message a=v10 b=v11)
	LANGSMITH_TRACING=false uv run python -m readmit.evals.ab --task $(or $(task),write_message) --a $(or $(a),v10) --b $(or $(b),v11)
