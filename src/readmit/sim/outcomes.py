"""Thirty-day outcomes for flagged live discharges, decided by the simulation, with a real outreach effect.

The world leaves live stays' outcomes open (milestone 1). Here each flagged stay gets:
  p0  the chance of an unplanned readmission within 30 days with usual care (the world's true risk)
  rr  how much a completed follow-up visit cuts that chance (heterogeneous: bigger for heart failure and COPD
      and for patients without a ride, small for dementia or substance use)
  p1  = p0 * (1 - rr), the chance if the patient attended a follow-up visit before the readmission date
  u   one seeded uniform draw, shared by both worlds: readmitted with usual care if u < p0, after a visit if
      u < p1. Both potential outcomes are known, so the true effect can grade any estimate (no real hospital
      can do this).

Holdout: scoring already put a seeded 20% of flagged patients in a holdout with no outreach (milestone 1). Their
outcomes are drawn the same way, which makes outreach vs holdout a randomized comparison.
Planned procedures are not readmissions (same label definition as the model) and aren't simulated here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from readmit import db
from readmit.sim.patients import seeded

BASE_REDUCTION = 0.25
MAX_DAYS = 30


@dataclass
class Outcome:
    encounter_id: str
    case_id: str | None  # None: holdout, no outreach case
    p0: float
    rr: float
    u: float
    readmit_at: datetime

    @property
    def p1(self) -> float:
        return self.p0 * (1 - self.rr)


def relative_reduction(dx: str, hidden: dict) -> float:
    """Who a follow-up visit helps most: med changes and fluid checks (HF, COPD), patients cut off from care
    (no ride, unstable housing). Least: dementia, substance use (the visit alone rarely changes the course)."""
    rr = BASE_REDUCTION
    rr += 0.15 * (dx in ("Heart failure", "COPD exacerbation"))
    rr += 0.10 * bool(hidden.get("transport_barrier")) + 0.05 * bool(hidden.get("housing_unstable"))
    rr -= 0.15 * bool(hidden.get("dementia")) + 0.10 * bool(hidden.get("substance_use"))
    return round(min(max(rr, 0.0), 0.6), 3)


def draw_outcomes(seed: int = 7) -> dict[str, Outcome]:
    """One outcome per flagged live stay (outreach and holdout). Empty if the world tables aren't there."""
    with db.transaction() as c:
        needed = ("truth.encounters", "truth.patients", "sim.encounters", "scoring.scores")
        if not all(db.one(c, f"select to_regclass('{t}') is not null as ok")["ok"] for t in needed):
            return {}
        rows = db.rows(
            c,
            """select s.encounter_id, s.holdout, te.true_risk, e.primary_dx_group as dx, e.discharge_datetime,
                      tp.transport_barrier, tp.housing_unstable, tp.dementia, tp.substance_use
               from scoring.scores s
               join truth.encounters te using (encounter_id)
               join (select distinct on (encounter_id) * from sim.encounters) e using (encounter_id)
               join truth.patients tp on tp.patient_id = s.patient_id
               where s.flagged""",
        )
    out = {}
    for r in rows:
        enc = r["encounter_id"]
        days = min(MAX_DAYS, 1 + int(seeded(seed, "gap", enc).gammavariate(1.4, 7)))
        out[enc] = Outcome(
            encounter_id=enc,
            case_id=None if r["holdout"] else f"C{enc[1:]}",
            p0=float(r["true_risk"]),
            rr=relative_reduction(r["dx"], r),
            u=seeded(seed, "outcome", enc).random(),
            readmit_at=r["discharge_datetime"] + timedelta(days=days),
        )
    return out


def save(outcomes: dict[str, Outcome]) -> None:
    """The draws (hidden truth) and an empty results table, so evaluations can grade the program later."""
    db.execute("create schema if not exists truth")
    db.execute(
        """create table if not exists truth.outcome_results (
               encounter_id text primary key, case_id text, holdout boolean, treated boolean,
               readmitted boolean, p0 double precision, p1 double precision, decided_at timestamp)"""
    )
    db.execute("truncate truth.outcome_results")
    draws = pd.DataFrame(
        [
            {
                "encounter_id": o.encounter_id,
                "case_id": o.case_id,
                "holdout": o.case_id is None,
                "p0": o.p0,
                "rr": o.rr,
                "p1": o.p1,
                "u": o.u,
                "readmit_at": o.readmit_at,
            }
            for o in outcomes.values()
        ],
        columns=["encounter_id", "case_id", "holdout", "p0", "rr", "p1", "u", "readmit_at"],
    )
    db.write(draws, "truth.outcome_draws")


def attended_before(case_id: str, at: datetime) -> bool:
    with db.transaction() as c:
        return bool(
            db.one(
                c,
                """select count(*) as n from outreach.appointments
                   where case_id = :id and status = 'completed' and start_at < :at""",
                id=case_id,
                at=at,
            )["n"]
        )


def decide(o: Outcome) -> dict:
    """The outcome at the readmission date: did a completed visit come first, and was the patient readmitted."""
    treated = bool(o.case_id) and attended_before(o.case_id, o.readmit_at)
    readmitted = o.u < (o.p1 if treated else o.p0)
    db.execute(
        """insert into truth.outcome_results values (:e, :c, :h, :t, :r, :p0, :p1, :at)
           on conflict (encounter_id) do nothing""",
        e=o.encounter_id,
        c=o.case_id,
        h=o.case_id is None,
        t=treated,
        r=readmitted,
        p0=o.p0,
        p1=o.p1,
        at=o.readmit_at,
    )
    return {"treated": treated, "readmitted": readmitted}


def report() -> dict:
    """Outreach vs holdout (randomized: an unbiased effect), attended vs not (self-selected: biased), and the
    true expected effect for comparison. Only stays whose outcome has been decided."""
    with db.transaction() as c:
        if not db.one(c, "select to_regclass('truth.outcome_results') is not null as ok")["ok"]:
            return {}
        rows = db.rows(c, "select * from truth.outcome_results")
    if not rows:
        return {}

    def rate(group):
        return (sum(r["readmitted"] for r in group) / len(group), len(group)) if group else (None, 0)

    outreach = [r for r in rows if not r["holdout"]]
    holdout = [r for r in rows if r["holdout"]]
    attended = [r for r in outreach if r["treated"]]
    not_attended = [r for r in outreach if not r["treated"]]
    true_effect = (
        sum(r["p0"] - (r["p1"] if r["treated"] else r["p0"]) for r in outreach) / len(outreach) if outreach else None
    )
    return {
        "outreach": rate(outreach),
        "holdout": rate(holdout),
        "attended": rate(attended),
        "not_attended": rate(not_attended),
        "true_effect": true_effect,
    }
