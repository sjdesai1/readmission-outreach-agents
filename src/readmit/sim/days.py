"""Run the outreach program over simulated days, one hour at a time.

    python -m readmit.sim.days --days 14            # rule-based agents (free, fast)
    python -m readmit.sim.days --days 3 --llm       # the real LLM agents (costs tokens, slower)

Each simulated hour:
  1. messages held for quiet hours whose time has come are sent
  2. in outreach hours (9 AM to 7 PM), every case whose next action is due gets one outreach turn
Patients don't reply yet (milestone 4 step 2 adds scripted patients), so every case runs the full
cadence and closes as unreachable. A summary per simulated day shows what happened.
"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta

from readmit import db
from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.outreach import cadence, store

HOUR = timedelta(hours=1)


async def simulate(gateway: Gateway, days: int, agents: dict | None = None, max_cases: int | None = None) -> list[dict]:
    """Advance the clock hour by hour for `days` days. Returns one summary row per simulated day."""
    t = store.now().replace(minute=0, second=0, microsecond=0)
    end = t + timedelta(days=days)
    daily: list[dict] = []
    day = Counter()
    day_start = t
    while t < end:
        store.set_now(t)
        day["held_sent"] += store.deliver_due()
        if cadence.in_outreach_hours(t):
            due = store.due_cases()
            for c in due[:max_cases] if max_cases else due:
                s = await run_turn(gateway, c["case_id"], "outreach_due", agents=agents)
                day[(s.get("outcome") or {}).get("action") or "skipped"] += 1
        t += HOUR
        if t - day_start >= timedelta(days=1):
            daily.append({"day": day_start.date().isoformat(), **day})
            day, day_start = Counter(), t
    return daily


def status_counts() -> dict:
    with db.transaction() as c:
        rows = db.rows(c, "select status, count(*) as n from outreach.cases group by status order by n desc")
    return {r["status"]: r["n"] for r in rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--llm", action="store_true", help="use the LLM agents (default: rule-based, no cost)")
    ap.add_argument("--start", help="set the simulation clock first, ISO datetime")
    ap.add_argument("--max-cases", type=int, help="at most this many turns per hour (keeps LLM runs small)")
    args = ap.parse_args()
    if args.start:
        store.set_now(datetime.fromisoformat(args.start))
    agents = None
    if args.llm:
        from readmit.agents.llm_agents import LLM_AGENTS

        agents = LLM_AGENTS
    daily = asyncio.run(simulate(Gateway.http(), args.days, agents, args.max_cases))
    keys = sorted({k for d in daily for k in d if k != "day"})
    print("  day         " + "  ".join(f"{k:>18}" for k in keys))
    for d in daily:
        print(f"  {d['day']}  " + "  ".join(f"{d.get(k, 0):>18}" for k in keys))
    print(f"\n  clock {store.now():%Y-%m-%d %H:%M} | cases {status_counts()}")


if __name__ == "__main__":
    main()
