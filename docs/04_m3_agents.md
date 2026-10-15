# Milestone 3: Multi-agent outreach in LangGraph

Goal: one patient turn at a time, decide what to do and say, and do it safely. Code controls the flow; LLMs make the judgment calls; every action goes through the MCP servers from milestone 2.

```
trigger (outreach due / patient reply)
  -> load_context -> input_guard ─┬─ STOP ──────────────> opt_out
                                  ├─ emergency ─────────> escalate (911 message + urgent nurse task)
                                  ├─ family member ─────> proxy_reply (fixed privacy template)
                                  ├─ phone, unverified ─> identity_check (year of birth, server-side)
                                  ├─ case with a nurse ─> hold_for_nurse (no AI reply)
                                  └─ reply ─────────────> reply_interpreter -> remember -> route by intent
  outreach due ─> care_planner -> scheduler -> message_writer
  wants_visit / reschedule / visit-type question -> scheduler -> message_writer
  question / barrier -> care_planner -> message_writer
  symptom / clinical question / wants a human / unclear twice / abusive -> escalate (nurse task)
  message_writer -> output_checks -> safety_reviewer -> send     (one rewrite, then escalate)
```

Run a turn: `uv run python -m readmit.agents.run C000856 --at 2026-09-10T09:00` (outreach), add `--reply "..." --channel phone` for a patient reply, `--stub` for rule-based agents, `--nurse-queue` for open nurse tasks.

## 1. Workflow, not an orchestrator LLM

- LangGraph `StateGraph`, one run = one turn for one case
- Routing is plain Python on the state: testable, auditable, no model deciding what happens next
- Typed `TurnState`; every node returns only the keys it changes; `path` records the nodes visited
- Rule nodes for anything with one right answer: context, guards, identity, send, escalate, opt out, proxy, memory
- See [ADR 0002](decisions/0002-deterministic-routing.md)

## 2. The agents

| Agent | Prompt | Tools | Output | Code checks after |
|---|---|---|---|---|
| Reply interpreter | interpret_reply v5 | none | intent, details, accepts, standing, confidence | low confidence becomes unclear; standing never for symptoms |
| Care planner | plan_care v1 | create_referral | channel, approach, talking points, referrals | channel must be consented; code files the referrals |
| Scheduler | schedule_step v2 | find_slots, book, reschedule | one action per step, max 5 | only slots it saw; slot_fits the request; code adds case id and keys |
| Message writer | write_message v6 | none | subject, body | output_checks: times, contacts, placeholders, claims, length, diagnosis, tone |
| Safety reviewer | review_message v2 | none | verdict, reasons, 6 checks | its own failed checks overrule "approve"; medical advice goes to a nurse |

- All calls through one LLM client: cache, model fallback, schema validation and repair, cost log, LangSmith trace
- Prompts are versioned files, never edited in place ([ADR 0005](decisions/0005-versioned-prompts.md))
- Every agent degrades safely: LLM down means a rule-based plan, a template message, the rule scheduler, or a flagged approve after the rule checks

## 3. Safety and privacy

- STOP words and emergencies (English and Spanish) handled by fixed rules before any LLM
- Phone replies need a year of birth, checked server-side; 3 misses lock and go to a nurse
- Family members get a fixed privacy reply, never details
- The LLM never sees the patient's name: `{first_name}` until the send node fills it in ([ADR 0004](decisions/0004-llm-never-sees-names.md))
- Diagnosis never in a message except on the logged-in portal
- Prompt injection flagged and passed to the interpreter as data
- Quiet hours 8 PM to 8 AM: normal messages held until morning, safety messages go now
- Booking guard: nothing contradicting the patient's request can be booked, whatever the agent decides

## 4. Nurse handoff

- Escalation creates a nurse task; kind and priority set by code ([ADR 0007](decisions/0007-nurse-handoff.md))
- Emergency: due now, 911 message first. Symptom: urgent, within the hour, acknowledged even at night
- Clinical question, callback: routine, 5 PM next business day with capacity (about 20 a day), weekends skipped
- The patient's acknowledgement quotes the task's real due time
- The AI pauses on the case; patient replies still pass the input guard (STOP, emergencies), anything else is held for the nurse
- Hand-back returns the case to booked if a visit is upcoming, else to awaiting reply

## 5. Patient memory

- Standing preferences only ("I work mornings"), from a fixed list of keys, per patient, across stays
- Saved by a rule node after the interpreter; unchanged values not re-saved
- Combined with each new request: days and time of day combine; within one dimension the new request wins

## 6. Tests

- `test_guards.py`: STOP, emergencies, family members, injection, invented times, tone, diagnosis, booking guard, memory merge
- `test_agent_graph.py`: whole graph with stub agents and in-memory MCP servers: paths, idempotent re-runs, identity, quiet hours, nurse handoff timing and capacity, pause and hand-back, emergencies while paused
- `test_llm_agents.py`: real agent code with a fake LLM: prompts, overrides, fallbacks, scheduler loop guards, reviewer verdicts, memory
- 117 tests; CI runs them on every PR

## 7. Live runs (Gemini, LangSmith)

One patient, phone channel, turn by turn on the simulation clock:

| Turn | What happened | Found |
|---|---|---|
| First outreach | 3 slots with the patient's own doctor, video and clinic labelled | |
| "YEAR. I work mornings..." | No year in the reply: asked again, nothing else done | |
| "1953. I work mornings..." | Verified, preference saved, afternoon slots offered | Only video offered |
| "Clinic visits instead?" | Reviewer flagged real doctor names as invented | Reviewer lacked the facts |
| Same, after fix | Read as a question, writer promised visits with no slots | Visit-type questions skipped the scheduler |
| Same, after fix | Afternoon clinic slots, approved after one rewrite | |
| "The 3 o'clock one works" | Booked exactly 3:00 PM | First draft re-asked "does this still work?" (LangSmith) |
| Ibuprofen question | No AI answer, nurse callback by Fri 5 PM quoted to the patient | Hand-back reset a booked patient |
| Swelling at 10:30 PM | Urgent task, acknowledged immediately despite quiet hours | |
| Chest pain while paused | Turn skipped: no 911 message | Safety gap, see postmortem |

Each finding became a PR with a regression test. Postmortem: [paused case skipped an emergency](postmortems/2026-10-15-paused-case-emergency.md).

## 8. What production would change

- Nurse queue in the EHR inbox, with paging for urgent and emergency tasks
- Identity verification through the patient portal or a one-time code, not year of birth
- Reviewer sampled and scored offline once its precision is measured (milestone 5)
- Durable execution (LangGraph checkpointer) for turns that wait on external events
