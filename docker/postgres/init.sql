-- Runs once, the first time the Postgres volume is created.
-- One server, three databases: Airflow's metadata, MLflow's tracking store, and the app's own data.
CREATE DATABASE airflow;
CREATE DATABASE mlflow;
CREATE DATABASE app;
