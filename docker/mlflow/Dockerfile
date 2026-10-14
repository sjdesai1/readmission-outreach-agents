# MLflow tracking server. psycopg2 lets it store run metadata in Postgres.
# Version must match the mlflow client in requirements.txt.
# SQLAlchemy pinned below 2.1: mlflow 3.1.x imports a pool class that 2.1 removed.
FROM python:3.14-slim
RUN pip install --no-cache-dir "mlflow==3.1.1" "psycopg2-binary>=2.9" "sqlalchemy>=2.0,<2.1"
