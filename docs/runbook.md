# Runbook

What to do when something goes wrong. Dashboard: http://localhost:3000 (Outreach operations). Alert emails: Mailpit, http://localhost:8025.

Each entry: what it means, how to check, how to fix, when to escalate.

## LLM spend near the daily cap

Alert: `LLM spend above 80% of the daily cap` (warning). Fires when real (uncached) LLM calls today cost over $0.80 of the $1.00 cap.

What it means
- At the cap, new LLM calls are refused and turns run on the rule-based agents (ADR 0008)
- Patients still get messages and every safety rule still holds; replies get blander and nuanced replies (indirect barriers, standing preferences) are read less well

Check
- Dashboard: "Calls, cost and latency by task": which task is spending
- A spike in one task with many failures: a retry loop
- A spike in real calls with few cache hits: a rerun with changed prompts (expected after a prompt change)

```sql
select task, count(*) filter (where not cached) as real_calls, sum(cost_usd) as usd
from ops.llm_calls where ts >= date_trunc('day', now()) group by 1 order by usd desc;
```

Fix
- Planned run (evals, A/B): let it finish; the cap holds
- Loop: stop the run, find the task in LangSmith or ops.llm_calls, fix, rerun (cached calls are free)
- Need more budget today: raise `LLM_DAILY_BUDGET_USD` in .env for that run only, and note it in the PR

Escalate
- Spend at the cap two days in a row with no planned runs: something is calling in a loop

## AI keeps failing on a case

Alert: `AI keeps failing on a case` (critical). Fires per case with 3 or more nurse review tasks.

What it means
- A turn failed (draft failed the checks twice, reviewer blocked it, scheduling error), went to a nurse, was handed back, and failed again
- From the third task the circuit breaker tells the nurse to handle the case directly (ADR 0009); the patient may have had no message for days
- Past causes: Spanish Tuesday dates read as March ([postmortem](postmortems/2026-10-19-tuesday-is-march.md)), the reviewer blocking good drafts over polish

Check
- Dashboard: "Cases the AI keeps failing on": the case ids
- The task reasons say why; the same reason repeated means one bug

```sql
select case_id, reason, count(*) from outreach.nurse_tasks
where kind = 'review' group by 1, 2 order by 3 desc limit 20;
```

- Reproduce: `make turn c=<case_id>` with the same clock, then read the trace

Fix
- Patient first: a nurse contacts the patient directly (the task says so)
- Then the bug: add the case to evals/turns.jsonl, fix, rerun `make eval-turns llm=1` and `make eval-conversations llm=1`
- One reason across many cases is a systemic bug: pause outreach (stop the scheduler) until fixed

Escalate
- More than 3 cases in a day with the same reason
- Any reason mentioning a symptom or emergency: clinical lead the same day

## Nurse queue backlog

No alert yet; watch "Overdue open tasks" on the dashboard.

What it means
- A routine task past its due time: the patient was told "a nurse will call by 5 PM" and nobody has
- Urgent tasks are due within the hour

Check
- "Open nurse tasks by priority" and "Overdue open tasks"
- `make nurse` opens the queue screen

Fix
- Work urgent first, then routine by due time; the queue screen orders them
- Routine capacity is 20 a day (`NURSE_DAILY_CAPACITY`); new routine tasks already roll to the next day with room, so a backlog means capacity is set too high for the real staff

Escalate
- Any overdue urgent task: immediately

## Interpreter tasks

Queue rows marked "interpreter: <language>" (ADR 0010).

What it means
- The patient wrote in, or asked for, a language the AI doesn't write; they got an acknowledgement in the language on file
- The AI does nothing more on the case

Fix
- Book an interpreter (phone or video) before calling; the machine translation under each message is for triage only
- If the call shows they read English or Spanish well, hand the case back (they can reply ENGLISH or ESPAÑOL to switch); otherwise resolve without handing back

## An MCP server is down

What it means
- Agents can't read or act; turns fail and go to nurses as review tasks

Check
- `make ps`: every mcp-* container up
- `make mcp-logs`

Fix
- `docker compose restart mcp-patient-data mcp-scheduling mcp-messaging mcp-referrals mcp-cases`
- After code changes in src/readmit/mcp or src/readmit/outreach, the servers need a restart to pick them up

## Messages bouncing

Watch "Delivery problems and opt-outs" on the dashboard.

What it means
- A wrong number or a dead email: the channel is marked bad and outreach moves to the next one (sms and phone fail together)
- A patient with no working channel goes to a nurse

Fix
- Nothing for one-offs; many in a day from one channel means the provider (or Mailpit) is down: `make smoke`

## A container won't restart after files changed

`docker compose restart` reuses the old bind mount; after a branch switch or pull replaced a mounted folder (e.g. docker/grafana) it fails with "error mounting ... no such file or directory". Recreate instead:

```bash
docker compose up -d --force-recreate grafana
```

## Useful commands

```bash
make ps                 # what's running
make smoke              # every service, including Grafana and its alert rules
make logs s=grafana     # one service's logs
make nurse              # nurse queue screen
make eval-turns llm=1   # did a fix break anything
```
