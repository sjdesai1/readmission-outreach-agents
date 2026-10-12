"""Postgres helpers. One engine, a few small functions, used by every module.

Schemas (namespaces inside the `app` database):
  sim      the simulated hospital's data: patients, encounters, notes
  truth    hidden answers, never read by the model or the agents
  cohort   labeled stays and the notes available at prediction time
  features model inputs, including what the LLM extracted
  scoring  risk scores, tiers, worklist
  ops      operational logs: every LLM call with tokens, cost, latency
"""
from functools import lru_cache

import pandas as pd
import sqlalchemy as sa

from readmit.config import settings

SCHEMAS = ["sim", "truth", "cohort", "features", "scoring", "ops"]


@lru_cache(maxsize=1)
def engine() -> sa.Engine:
    return sa.create_engine(settings.db_url, pool_pre_ping=True)


def ensure_schemas() -> None:
    with engine().begin() as c:
        for s in SCHEMAS:
            c.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {s}"))


def write(df: pd.DataFrame, table: str, if_exists: str = "replace") -> None:
    """Write a DataFrame to "schema.table". Replace by default, so reruns are idempotent."""
    schema, name = table.split(".")
    # Postgres allows ~65k parameters per statement: rows per batch x columns must stay under it
    chunk = max(1, 60000 // max(1, len(df.columns)))
    df.to_sql(name, engine(), schema=schema, if_exists=if_exists, index=False, chunksize=chunk, method="multi")


def read(sql: str, **params) -> pd.DataFrame:
    return pd.read_sql(sa.text(sql), engine(), params=params)


def execute(sql: str, **params) -> None:
    with engine().begin() as c:
        c.execute(sa.text(sql), params)
