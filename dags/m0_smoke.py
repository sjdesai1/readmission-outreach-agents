"""Milestone 0: proves Airflow can run project code against every service.

Pattern used by every DAG in this project:
  Airflow only orchestrates. Each task runs project code with the separate
  project venv (/opt/app-venv), so Airflow's dependencies never clash with
  the ML / agent libraries.
"""

from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag

APP_PY = "cd /opt/project && /opt/app-venv/bin/python"


@dag(
    dag_id="m0_smoke",
    schedule=None,  # run on demand only
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["milestone-0"],
)
def m0_smoke():
    BashOperator(
        task_id="smoke_all_services",
        bash_command=f"{APP_PY} -m readmit.smoke",
    )


m0_smoke()
