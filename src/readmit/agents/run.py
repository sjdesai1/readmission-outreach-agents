"""Run one turn by hand against the running stack (MCP servers over HTTP).

    python -m readmit.agents.run C000856 --at 2026-09-10T09:00                 # outreach due
    python -m readmit.agents.run C000856 --reply "yes please" --channel sms    # patient replied
    python -m readmit.agents.run C000856 --stub ...                            # rule-based agents, no LLM

--at moves the simulation clock first. --reply records the patient's message (as the simulator would), then
runs an inbound turn. Prints the path through the graph, the message, and what the turn did.
"""
import argparse
import asyncio
import json
from datetime import datetime

from readmit.agents.gateway import Gateway
from readmit.agents.graph import run_turn
from readmit.agents.llm_agents import LLM_AGENTS
from readmit.outreach import store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("case_id")
    ap.add_argument("--at", help="set the simulation clock first, ISO datetime")
    ap.add_argument("--reply", help="patient's reply text (runs an inbound turn)")
    ap.add_argument("--channel", default="sms", help="channel of the reply")
    ap.add_argument("--stub", action="store_true", help="rule-based stub agents instead of the LLM agents")
    args = ap.parse_args()

    if args.at:
        store.set_now(datetime.fromisoformat(args.at))
    trigger = "inbound_reply" if args.reply else "outreach_due"
    if args.reply:
        store.record_inbound(args.case_id, args.channel, args.reply)

    s = asyncio.run(run_turn(Gateway.http(), args.case_id, trigger, args.reply,
                             args.channel if args.reply else None, agents=None if args.stub else LLM_AGENTS))
    print(f"  clock    {s.get('now')}")
    print(f"  path     {' -> '.join(s.get('path', []))}")
    if s.get("skip"):
        print(f"  skipped  {s['skip']}")
    if s.get("intent"):
        print(f"  intent   {s['intent']}")
    if s.get("plan"):
        print(f"  plan     {json.dumps(s['plan'], default=str)}")
    if s.get("draft"):
        print(f"  message  {s['draft']['body']}")
    if s.get("problems"):
        print(f"  problems {s['problems']}")
    print(f"  outcome  {json.dumps(s.get('outcome'), default=str)}")


if __name__ == "__main__":
    main()
