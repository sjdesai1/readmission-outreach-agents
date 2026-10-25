"""From a fresh clone to something to click through, in one command.

    make demo                 # rule-based agents: no API key, no cost
    make demo llm=1           # the LLM agents (needs GEMINI_API_KEY; at most 5 outreach turns an hour, and the
                              # daily budget, LLM_DAILY_BUDGET_USD, caps the cost: over it, rule-based)
    make demo rebuild=1       # rebuild the world and the model too

Steps (the Makefile starts the Docker stack first):
  1. wait for Postgres and the five MCP servers
  2. build the world and the model, only if they're missing: world, cohort, social factors from notes (the LLM
     with a key, keyword rules without one), train, score, post-discharge signals, re-score models
  3. reset outreach and run the program over simulated days (121 by default: every case opens and gets its
     30-day outcome)
  4. print what happened, cases worth opening, and the links
"""

import argparse
import os
import socket
import subprocess
import sys
import time

from readmit import db
from readmit.config import settings
from readmit.mcp.common import PORTS
from readmit.sim import clock

# the live period plus 30 days (121): every case opens, plays out, and gets its 30-day outcome
DAYS = (clock.LIVE_END - clock.LIVE_START).days + 1 + 30
LINKS = {
    "Case timeline": "make timeline     -> http://localhost:8502",
    "Nurse queue": "make nurse        -> http://localhost:8501",
    "Dashboard": "http://localhost:3000 (Grafana)",
    "Emails sent": "http://localhost:8025 (Mailpit)",
    "Models": "http://localhost:5000 (MLflow)",
}


def build_steps(has_key: bool) -> list[list[str]]:
    """The milestone 1 pipeline, in order. Without a key the social factors come from keyword rules."""
    return [
        ["readmit.sim.generate"],
        ["readmit.data.cohort"],
        ["readmit.extract.social"] if has_key else ["readmit.extract.social", "--rules"],
        ["readmit.model.train"],
        ["readmit.model.score"],
        ["readmit.sim.signals"],
        ["readmit.model.landmark"],
    ]


def built() -> bool:
    """Scores for live discharges exist: the world and the model are built."""
    with db.transaction() as c:
        if not db.one(c, "select to_regclass('scoring.scores') is not null as ok")["ok"]:
            return False
        return db.one(c, "select count(*) as n from scoring.scores")["n"] > 0


def wait_for_stack(timeout_s: int = 240) -> None:
    """Postgres answers and every MCP server accepts connections (they start after Postgres)."""
    deadline = time.monotonic() + timeout_s
    waiting = ["postgres", *PORTS]
    while waiting and time.monotonic() < deadline:
        still = []
        for name in waiting:
            try:
                if name == "postgres":
                    db.engine.cache_clear()  # a failed first connection mustn't stick
                    with db.transaction() as c:
                        db.one(c, "select 1 as ok")
                else:
                    socket.create_connection((settings.mcp_host, PORTS[name]), timeout=2).close()
            except Exception:  # noqa: BLE001 - not up yet, whatever the reason
                still.append(name)
        if still and still != waiting:
            print(f"  waiting for: {', '.join(still)}")
        waiting = still
        if waiting:
            time.sleep(3)
    if waiting:
        sys.exit(f"  not up after {timeout_s}s: {', '.join(waiting)}. Check `make ps` and `make logs s=<service>`.")


def run(module_args: list[str], env: dict) -> None:
    print(f"\n==> {' '.join(module_args)}", flush=True)
    done = subprocess.run([sys.executable, "-m", *module_args], env=env, check=False)
    if done.returncode:
        sys.exit(f"  step failed: {' '.join(module_args)} (exit {done.returncode})")


def summary() -> dict:
    """What happened in the program: cases by status, messages, nurse work, visits, readmissions."""
    with db.transaction() as c:
        status = {r["status"]: r["n"] for r in db.rows(c, """
            select case when c.opened_at > k.now then 'not discharged yet' else c.status end as status, count(*) as n
            from outreach.cases c cross join outreach.sim_clock k group by 1 order by n desc""")}  # fmt: skip
        n = db.one(
            c,
            """select (select count(*) from outreach.messages where direction = 'outbound') as sent,
                      (select count(*) from outreach.messages where direction = 'inbound') as replies,
                      (select count(*) from outreach.nurse_tasks) as nurse_tasks,
                      (select count(*) from outreach.nurse_tasks where priority = 'urgent') as urgent,
                      (select count(*) from outreach.appointments) as booked,
                      (select count(*) from outreach.events where type = 'visit_completed') as attended,
                      (select count(*) from outreach.events where type = 'readmitted') as readmitted""",
        )
    return {"cases": sum(status.values()), "by_status": status, **n}


def report(s: dict, examples: list[dict]) -> str:
    lines = [
        "",
        "==> Demo ready",
        f"  {s['cases']} outreach cases: " + ", ".join(f"{k} {v}" for k, v in s["by_status"].items()),
        (
            f"  {s['sent']} messages sent, {s['replies']} patient replies, {s['booked']} visits booked, "
            f"{s['attended']} attended"
        ),
        f"  {s['nurse_tasks']} nurse tasks ({s['urgent']} urgent), {s['readmitted']} readmitted so far",
        "",
        "  Cases worth opening in the timeline:",
        *[f"    {x['case_id']:<10} {x['label']}" for x in examples[:5]],
        "",
        *[f"  {k:<14} {v}" for k, v in LINKS.items()],
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--llm", action="store_true", help="the LLM agents (needs GEMINI_API_KEY)")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the world and the model even if they exist")
    args = ap.parse_args()
    has_key = bool(settings.gemini_api_key)
    if args.llm and not has_key:
        sys.exit("  llm=1 needs GEMINI_API_KEY in .env. Leave it off for the free rule-based demo.")
    env = {**os.environ, "LANGSMITH_TRACING": os.environ.get("LANGSMITH_TRACING", "false") if args.llm else "false"}

    print("==> Waiting for the stack")
    wait_for_stack()
    if args.rebuild or not built():
        print("==> Building the world and the model (first run: a few minutes)")
        if not has_key:
            print("  no GEMINI_API_KEY: social factors from keyword rules instead of the LLM")
        for step in build_steps(has_key):
            run(step, env)
    else:
        print("==> World and model already built (rebuild=1 to redo)")
    run(["readmit.outreach.setup"], env)
    run(["readmit.sim.days", "--days", str(args.days), *(["--llm", "--max-cases", "5"] if args.llm else [])], env)

    from readmit.viewer import timeline

    print(report(summary(), timeline.interesting()))


if __name__ == "__main__":
    main()
