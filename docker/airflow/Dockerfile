# Airflow image with a SEPARATE virtual environment for the project code.
# Why: Airflow pins its own dependency versions. The ML and agent libraries
# (XGBoost, LangGraph, LiteLLM...) would conflict with them. Keeping them in
# /opt/app-venv means Airflow only orchestrates, and tasks run project code with
# /opt/app-venv/bin/python. Same idea as Airflow's ExternalPythonOperator, or
# running each task in its own container in production.
ARG AIRFLOW_VERSION
FROM apache/airflow:${AIRFLOW_VERSION}-python3.11

USER root
RUN python -m venv /opt/app-venv && chown -R airflow:0 /opt/app-venv
USER airflow

COPY requirements.txt /tmp/requirements.txt
RUN /opt/app-venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/app-venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# project source is mounted at /opt/project at runtime (see docker-compose.yml)
ENV PYTHONPATH=/opt/project/src
