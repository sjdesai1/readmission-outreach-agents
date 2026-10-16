# Postmortem: medical question kept getting outreach after a reset

Date: 2026-10-15 · Severity: high (no nurse follow-up, wrong callback time) · Environment: simulation only, found by the persona report card

## Summary
- 90-day simulation: one med_question patient ended "closed: no reply after 3 attempts" instead of with a nurse
- Thread: the patient asked about ibuprofen, got a callback promise "by Fri Sep 11", then two more outreach calls
- The callback time came from a task created in an earlier run

## Timeline
- Persona report card showed med_question: escalated 3, closed 1
- One query pulled close reason, nurse task reason and thread for the outliers
- The nurse task reason was "chest pain", from the manual runs hours earlier

## Root causes
- `make outreach-setup` truncated a hand-written list of tables; nurse_tasks and patient_memory, added later, weren't on it
- Scripted patients are deterministic: the same reply at the same simulated time gives the same turn key
- create_nurse_task saw the old task's idempotency key and returned it as "already created", skipping the status change
- So the case was never paused, outreach continued, and the patient was quoted a stale due time

## Fix
- Setup now finds and truncates every table in the outreach schema at run time
- Regression test: setup leaves every outreach table empty, including nurse tasks and memory

## What went well
- The persona report card made one bad case out of 46 visible
- Idempotency did exactly what it promises; the bug was stale data, not the key design

## Follow-ups
- Milestone 5 evals: assert every persona's expected end state on each simulation run
