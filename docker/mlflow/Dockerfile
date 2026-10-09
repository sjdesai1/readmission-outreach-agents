# MLflow tracking server. psycopg2 lets it store run metadata in Postgres.
# Version must match the mlflow client in requirements.txt.
FROM python:3.11-slim
RUN pip install --no-cache-dir "mlflow==3.1.1" "psycopg2-binary>=2.9"
