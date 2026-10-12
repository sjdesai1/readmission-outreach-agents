# Shortcuts. Each target is just the docker / python command written next to it.
.PHONY: setup up down reset ps logs smoke smoke-airflow test lint check \
        world cohort extract extract-eval train score pipeline pipeline-airflow

setup:          ## one-time: create .env and the local Python environment
	@test -f .env || cp .env.example .env
	uv sync --extra dev

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

check:          ## run before every push: lint + tests
	uv run ruff check . && uv run pytest -q

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

pipeline: world cohort extract extract-eval train score   ## everything, in order

pipeline-airflow:  ## the same pipeline, run by Airflow
	docker compose exec airflow airflow dags test m1_build_world
