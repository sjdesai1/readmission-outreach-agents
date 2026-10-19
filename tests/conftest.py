"""Test-wide settings and the shared Postgres fixture.

Tests never send traces to LangSmith, even if .env turns tracing on.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"

import dataclasses

import pytest
import sqlalchemy as sa

from readmit import db
from readmit.evals import fixture
from readmit.outreach import store

NOW = fixture.NOW


@pytest.fixture
def pg(monkeypatch):
    """Fresh outreach tables in app_test for every test."""
    test_settings = dataclasses.replace(db.settings, db_name="app_test")
    admin = sa.create_engine(test_settings.db_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            if not c.execute(sa.text("select 1 from pg_database where datname = 'app_test'")).first():
                c.execute(sa.text("create database app_test"))
    except Exception as e:  # noqa: BLE001 - no database available: skip, don't fail
        pytest.skip(f"Postgres not reachable: {e}")
    finally:
        admin.dispose()  # close its connection now, not whenever garbage collection gets to it
    monkeypatch.setattr(db, "settings", test_settings)
    db.engine.cache_clear()
    monkeypatch.setattr(store, "_send_email", lambda to, subject, body: None)
    fixture.load()  # the shared two-patient world (src/readmit/evals/fixture.py)
    yield
    db.engine().dispose()  # each test's pool is closed, so 100+ tests never exhaust Postgres connections
    db.engine.cache_clear()


@pytest.fixture
def add_slot(pg):
    """Add one more open slot, for a test that needs a specific time (the fixture's are all 9 AM)."""

    def add(sid, start, mode="in_person"):
        fixture.add_slot(sid, start, mode)

    return add
