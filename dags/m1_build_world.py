"""Milestone 1 pipeline: simulated hospital -> labeled cohort -> LLM extraction -> model -> live scores.

Every task runs project code in the project's own venv (/opt/app-venv), same as m0_smoke.
Extraction is cheap on reruns: the LLM cache lives in the mounted project folder (data/llm_cache).
"""
from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag

APP = "cd /opt/project && /opt/app-venv/bin/python -m"


@dag(
    dag_id="m1_build_world",
    schedule=None,                       # on demand; daily scheduling comes with the live simulation
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=1)},
    tags=["milestone-1"],
)
def m1_build_world():
    generate = BashOperator(task_id="generate_hospital", bash_command=f"{APP} readmit.sim.generate")
    cohort = BashOperator(task_id="build_cohort", bash_command=f"{APP} readmit.data.cohort")
    extract = BashOperator(task_id="extract_social_factors", bash_command=f"{APP} readmit.extract.social",
                           execution_timeout=timedelta(hours=2))
    extract_eval = BashOperator(task_id="evaluate_extraction", bash_command=f"{APP} readmit.extract.evaluate")
    train = BashOperator(task_id="train_models", bash_command=f"{APP} readmit.model.train")
    score = BashOperator(task_id="score_live_discharges", bash_command=f"{APP} readmit.model.score")

    generate >> cohort >> extract >> extract_eval >> train >> score


m1_build_world()
