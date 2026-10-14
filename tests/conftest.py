"""Test-wide settings and the shared Postgres fixture.

Tests never send traces to LangSmith, even if .env turns tracing on.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from readmit import db
from readmit.outreach import store

NOW = datetime(2026, 9, 9, 9, 0)
SEED = """
create schema if not exists sim; create schema if not exists cohort; create schema if not exists scoring;
drop table if exists sim.patients, cohort.stays, scoring.scores;
create table sim.patients (patient_id text, first_name text, preferred_language text, email text, mobile_phone text,
    has_portal_account bool, portal_opt_in bool, sms_opt_in bool, email_opt_in bool, phone_opt_in bool,
    telehealth_opt_in bool, telehealth_capable bool, preferred_channel text, contact_window text, birth_year int);
create table cohort.stays (encounter_id text, primary_dx_group text, discharge_datetime timestamp,
    discharge_disposition text, followup_scheduled_7d bool, age int);
create table scoring.scores (encounter_id text, risk float, tier text, top_drivers text, barriers text);
insert into sim.patients values
  ('P1', 'Maria', 'English', 'maria@example.com', null, false, false, false, true, false, true, true, 'email', 'morning',
   1954),
  ('P2', 'Jose', 'Spanish', null, '+17145550100', true, true, true, false, true, false, false, 'sms', 'evening', 1968);
insert into cohort.stays values ('E1', 'Heart failure', '2026-09-07 12:00', 'Home', false, 72),
                                ('E2', 'Pneumonia', '2026-09-07 15:00', 'Home', false, 58);
insert into scoring.scores values ('E1', 0.41, 'high', '[]', '["transport_barrier"]'), ('E2', 0.2, 'medium', '[]', '[]');
"""


def slot(sid, start, mode="in_person", status="open"):
    return {
        "slot_id": sid,
        "provider_id": "PRV01",
        "start_at": start,
        "end_at": start + timedelta(minutes=30),
        "mode": mode,
        "status": status,
    }


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
    monkeypatch.setattr(db, "settings", test_settings)
    db.engine.cache_clear()
    monkeypatch.setattr(store, "_send_email", lambda to, subject, body: None)
    with db.engine().begin() as c:
        c.exec_driver_sql(SEED)
        c.exec_driver_sql("drop schema if exists outreach cascade")
        c.exec_driver_sql((Path(store.__file__).parent / "schema.sql").read_text())
        c.exec_driver_sql("insert into outreach.providers values ('PRV01', 'Dr. Test', 'primary care')")
    with db.transaction() as c:
        for s in [
            slot("S1", NOW + timedelta(days=1)),
            slot("S2", NOW + timedelta(days=2)),
            slot("S3", NOW + timedelta(days=3), mode="telehealth"),
            slot("S4", NOW + timedelta(hours=1)),
            slot("S5", NOW + timedelta(days=4), status="held_other"),
        ]:
            db.rows(
                c,
                """insert into outreach.slots (slot_id, provider_id, start_at, end_at, mode, status)
                          values (:slot_id, :provider_id, :start_at, :end_at, :mode, :status)""",
                **s,
            )
        for cid, eid, pid in [("C1", "E1", "P1"), ("C2", "E2", "P2")]:
            db.rows(
                c,
                """insert into outreach.cases (case_id, encounter_id, patient_id, opened_at, tier, risk)
                          values (:cid, :e, :p, :t, 'high', 0.4)""",
                cid=cid,
                e=eid,
                p=pid,
                t=NOW - timedelta(days=1),
            )
    store.set_now(NOW)
    yield
    db.engine.cache_clear()
