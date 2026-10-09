# Shortcuts. Each target is just the docker / python command written next to it.
.PHONY: setup up down reset ps logs smoke smoke-airflow test lint

setup:          ## one-time: create .env and the local Python environment
	@test -f .env || cp .env.example .env
	uv sync --extra dev

up:             ## build and start the whole stack
	docker compose up -d --build

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
