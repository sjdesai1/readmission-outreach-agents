# Postmortem: Spanish messages with a Tuesday slot never sent

Date: 2026-10-19 · Severity: high (Spanish patients offered Tuesday times got no outreach; nurse queue flooded) · Environment: simulation only, found by the conversation evals

## Summary
- Conversation evals: silent Jose ended with 13 nurse review tasks, never closed; LLM runs showed the same for most Jose conversations
- Every task: "draft failed checks twice: mentions a date (mar 15) that is not one of the offered slots"
- "mar 15 sep" is Spanish for Tuesday Sep 15; the date check read "mar 15" as March 15

## Timeline
- First conversation eval run: 2/20 with the LLM agents, 13 to 21 review tasks per Jose conversation
- Added nurse task reasons and the last messages to the failure report
- Rerun on one persona: one reason, repeated 13 times, named the date

## Root causes
- The English date pattern was case-insensitive and "mar" is both March and martes (Tuesday)
- Spanish short dates ("jue 10 sep") were not checked at all: the Spanish pattern only knew "10 de septiembre". The same gap that caused the false alarm hid real invented dates
- No limit on retries: a turn failed, went to a nurse, the nurse handed it back, the next turn failed the same way
- Per-turn evals missed it: their slots never fell on a Tuesday

## Fix
- English dates followed by a Spanish month are skipped; Spanish short dates are checked (weekday, day, month)
- Circuit breaker: from the third review task on one case, the task says "the AI keeps failing on this case; handle it directly", and the simulated nurses take the case over instead of handing it back
- Regression tests for both

## What went well
- The conversation evals found what single-turn evals could not: a bug that only shows on some days of the week, and a loop that only shows over days
- One added line in the failure report (nurse task reasons, counted) turned 21 opaque failures into one root cause

## Follow-ups
- Per-turn evals: add Spanish cases with slots on every weekday
- Dashboard alert (step 6): review tasks per case above 2
