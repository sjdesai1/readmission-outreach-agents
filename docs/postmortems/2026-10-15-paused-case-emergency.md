# Postmortem: emergency reply skipped while a nurse had the case

Date: 2026-10-15 · Severity: critical (no emergency response) · Environment: simulation only, found in manual testing

## Summary
- Patient reported swelling at 10:30 PM: urgent nurse task created, case paused for the nurse
- Ten minutes later: "Now I have chest pain too."
- The turn was skipped: no 911 message, no emergency task

## Timeline
- Milestone 3 step 6 live runs, testing replies on a paused case on purpose
- Prediction written before the run: the skip would swallow the emergency
- Confirmed in one run; fix, tests and live replay merged the same evening

## Root causes
- The pause was implemented by adding "escalated" to the closed statuses in load_context
- Closed means "do nothing"; paused should mean "the AI doesn't converse"
- The input guard (STOP, emergencies) ran after load_context, so it never saw the reply
- Tests checked that a paused case stays quiet, not what happens to a dangerous reply

## Fix
- Separate PAUSED from CLOSED: outreach turns on a paused case are still skipped
- Patient replies on a paused case go through the input guard: STOP and emergencies handled with fixed messages, no LLM
- Anything else goes to hold_for_nurse: no AI reply, a note on the case
- Emergency tasks now due immediately and listed first in the nurse queue
- Regression tests replay the exact sequence

## What went well
- Found by deliberately testing the riskiest edge, not by luck
- Safety handling was already rule-based, so the fix was routing, not prompts

## Follow-ups
- Red-team suite (milestone 5): dangerous replies in every case state, not just the happy path
- Nurse queue screen (milestone 4): new patient messages on a paused case shown on the task
