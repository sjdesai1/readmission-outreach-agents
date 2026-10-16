"""Run the outreach program over simulated days, one hour at a time, with scripted patients replying.

    python -m readmit.sim.days --days 14            # rule-based agents (free, fast)
    python -m readmit.sim.days --days 3 --llm       # the real LLM agents (costs tokens, slower)

Each simulated hour:
  1. messages held for quiet hours whose time has come are sent
  2. visits that ended an hour ago are marked attended or no-show (seeded, by persona); stays whose readmission
     date has come get their 30-day outcome (sim/outcomes.py: a completed visit lowers the chance)
  3. at 7 AM the clinic cancels a few upcoming visits (doctor out, schedule changes)
  4. patient replies that are due arrive, and each gets an inbound turn (any hour: people text at night)
  5. in outreach hours (9 AM to 7 PM): day-before reminders, then one outreach turn per due case
  6. every message delivered this hour reaches its scripted patient (or bounces, if that contact is wrong),
     who may schedule a reply
Prints a summary per simulated day and, at the end, how each persona's cases ended.
"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta

from readmit import db
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.outreach import cadence, store
from readmit.sim import outcomes as world_outcomes
from readmit.sim.outcomes import Outcome
from readmit.sim.patients import Patient, Reply, attends, bad_contacts, respond, seeded

HOUR = timedelta(hours=1)
CLINIC_CANCEL_RATE = 0.03  # chance per upcoming visit, checked each morning
DELIVERED = ("sent", "delivered", "queued")


def load_patients(
    personas: dict[str, str] | None = None, bad: dict[str, tuple[str, ...]] | None = None, seed: int = 7
) -> dict[str, Patient]:
    """One scripted patient per case. Personas come from the hidden truth table, broken contacts are seeded per
    patient (both can be passed in by tests)."""
    with db.transaction() as c:
        rows = db.rows(
            c,
            """select k.case_id, k.patient_id, p.birth_year from outreach.cases k
               join sim.patients p using (patient_id)""",
        )
        if personas is None:
            has_truth = db.one(c, "select to_regclass('truth.patients') is not null as ok")["ok"]
            truth = db.rows(c, "select patient_id, persona from truth.patients") if has_truth else []
            by_patient = {r["patient_id"]: r["persona"] for r in truth}
            personas = {r["case_id"]: by_patient.get(r["patient_id"], "engaged") for r in rows}
    return {
        r["case_id"]: Patient(
            r["case_id"],
            personas.get(r["case_id"], "silent"),
            r["birth_year"],
            bad_channels=bad.get(r["case_id"], ()) if bad is not None else bad_contacts(r["patient_id"], seed),
        )
        for r in rows
    }


def new_deliveries(seen: set[str]) -> list[dict]:
    """Outbound messages that reached the patient and haven't been shown to them yet."""
    with db.transaction() as c:
        rows = db.rows(
            c,
            """select message_id, case_id, channel, body from outreach.messages
               where direction = 'outbound' and status = any(:ok) order by created_at, message_id""",
            ok=list(DELIVERED),
        )
    fresh = [r for r in rows if r["message_id"] not in seen]
    seen.update(r["message_id"] for r in fresh)
    return fresh


async def simulate(
    gateway: Gateway,
    days: int,
    agents: dict | None = None,
    max_cases: int | None = None,
    personas: dict[str, str] | None = None,
    seed: int = 7,
    no_show: dict[str, float] | None = None,
    cancel_rate: float = CLINIC_CANCEL_RATE,
    bad: dict[str, tuple[str, ...]] | None = None,
    outcomes: dict[str, Outcome] | None = None,
) -> list[dict]:
    """Advance the clock hour by hour for `days` days. Returns one summary row per simulated day."""
    t = store.now().replace(minute=0, second=0, microsecond=0)
    end = t + timedelta(days=days)
    patients = load_patients(personas, bad, seed)
    pending_outcomes = dict(world_outcomes.draw_outcomes(seed) if outcomes is None else outcomes)
    world_outcomes.save(pending_outcomes)
    pending: list[Reply] = []
    seen: set[str] = set()
    daily: list[dict] = []
    day, day_start = Counter(), t
    while t < end:
        store.set_now(t)
        day["held_sent"] += store.deliver_due()
        for v in store.visits_to_settle():
            persona = patients[v["case_id"]].persona if v["case_id"] in patients else "engaged"
            came = attends(persona, f"{v['case_id']}|{v['start_at']}", seed, (no_show or {}).get(persona))
            store.record_visit(v["appointment_id"], came)
            day["visit_attended" if came else "visit_no_show"] += 1
        for enc, o in sorted(pending_outcomes.items(), key=lambda kv: kv[1].readmit_at):
            if o.readmit_at > t:
                break
            del pending_outcomes[enc]
            if world_outcomes.decide(o)["readmitted"]:
                day["readmitted" if o.case_id else "readmitted_holdout"] += 1
                if o.case_id:
                    store.mark_readmitted(o.case_id, o.readmit_at)
        if t.hour == 7:
            for a in upcoming_visits():
                if seeded(seed, "clinic", a["case_id"], a["start_at"], t.date()).random() < cancel_rate:
                    store.clinic_cancel(a["appointment_id"], "doctor unavailable")
                    day["clinic_cancelled"] += 1
        for r in sorted((r for r in pending if r.due_at <= t), key=lambda r: r.due_at):
            pending.remove(r)
            store.record_inbound(r.case_id, r.channel, r.text)
            s = await run_turn(gateway, r.case_id, "inbound_reply", r.text, r.channel, agents)
            day["reply:" + ((s.get("outcome") or {}).get("action") or "skipped")] += 1
        if cadence.in_outreach_hours(t):
            for r in store.reminders_due():
                s = await run_turn(gateway, r["case_id"], "reminder", agents=agents)
                day[(s.get("outcome") or {}).get("action") or "skipped"] += 1
            due = store.due_cases()
            for c in due[:max_cases] if max_cases else due:
                s = await run_turn(gateway, c["case_id"], "outreach_due", agents=agents)
                day[(s.get("outcome") or {}).get("action") or "skipped"] += 1
        for m in new_deliveries(seen):
            patient = patients.get(m["case_id"])
            if patient and m["channel"] in patient.bad_channels:
                reason = "email bounced" if m["channel"] == "email" else "wrong number"
                store.delivery_failed(m["message_id"], reason)
                day["delivery_failed"] += 1
                continue
            if patient and (reply := respond(patient, m, t, seed)):
                pending.append(reply)
        t += HOUR
        if t - day_start >= timedelta(days=1):
            daily.append({"day": day_start.date().isoformat(), **day})
            day, day_start = Counter(), t
    return daily


def upcoming_visits() -> list[dict]:
    """Booked visits still ahead: the clinic can cancel any of them, even the same morning."""
    with db.transaction() as c:
        return db.rows(
            c,
            """select appointment_id, case_id, start_at from outreach.appointments
               where status = 'booked' and start_at > :t order by start_at, case_id""",
            t=store.now(c),
        )


def outcomes_by_persona(personas: dict[str, str] | None = None) -> dict[str, Counter]:
    """How each persona's cases ended up: the simulation's report card."""
    patients = load_patients(personas)
    with db.transaction() as c:
        status = {r["case_id"]: r["status"] for r in db.rows(c, "select case_id, status from outreach.cases")}
    out: dict[str, Counter] = {}
    for cid, p in patients.items():
        out.setdefault(p.persona, Counter())[status[cid]] += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--llm", action="store_true", help="use the LLM agents (default: rule-based, no cost)")
    ap.add_argument("--start", help="set the simulation clock first, ISO datetime")
    ap.add_argument("--max-cases", type=int, help="at most this many outreach turns per hour (keeps LLM runs small)")
    args = ap.parse_args()
    if args.start:
        store.set_now(datetime.fromisoformat(args.start))
    agents = None
    if args.llm:
        from readmit.agents.llm_agents import LLM_AGENTS

        agents = LLM_AGENTS
    daily = asyncio.run(simulate(Gateway.http(), args.days, agents, args.max_cases))
    keys = sorted({k for d in daily for k in d if k != "day"})
    width = max(len(k) for k in keys) if keys else 8
    for k in keys:
        print(f"  {k:<{width}}  " + " ".join(f"{d.get(k, 0):>3}" for d in daily))
    print(f"  {'(day)':<{width}}  " + " ".join(d["day"][-2:].rjust(3) for d in daily))
    print(f"\n  clock {store.now():%Y-%m-%d %H:%M}")
    r = world_outcomes.report()
    if r:

        def fmt(pair):
            rate_, n = pair
            return f"{rate_:.0%} of {n}" if n else "none yet"

        print("  30-day readmissions (decided so far):")
        print(f"    outreach {fmt(r['outreach'])}   holdout {fmt(r['holdout'])}   (randomized: an honest effect)")
        print(f"    attended a visit {fmt(r['attended'])}   did not {fmt(r['not_attended'])}   (self-selected: biased)")
        if r["true_effect"] is not None:
            print(f"    true expected effect of outreach for this cohort: {r['true_effect']:.1%} points fewer")
    print("  how each persona's cases ended:")
    for persona, counts in sorted(outcomes_by_persona().items()):
        print(f"    {persona:<18} " + ", ".join(f"{s} {n}" for s, n in counts.most_common()))


if __name__ == "__main__":
    main()
