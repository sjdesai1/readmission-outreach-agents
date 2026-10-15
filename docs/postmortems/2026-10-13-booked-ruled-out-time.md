# Postmortem: booked a time the patient had ruled out

Date: 2026-10-13 · Severity: high (wrong appointment) · Environment: simulation only, found in manual testing

## Summary
- Patient replied: "cant do noon, anything thurs after 2? video is fine"
- System booked Thu 12:00 PM, in the clinic, and sent a confirmation
- Every automated check passed

## Timeline
- Live turn run against Gemini after milestone 3 step 2
- Output reviewed by hand: booking contradicted the reply
- Root cause found the same day; fix, tests and replay merged the same day

## Root causes
- Interpreter prompt v1 defined accepts_offer as "agrees to a visit or picks an option": a request for other times read as acceptance
- Extraction had part_of_day only: "after 2" became afternoon, which includes noon
- Stub scheduler booked the first offered slot whenever accepts was true, ignoring extracted details
- Output checks validated times against offered and booked slots, not against what the patient asked for

## Fix
- interpret_reply v2: accepts only for a specific offered time; exact earliest_time / latest_time bounds
- slot_fits guard: no slot that contradicts the requested weekday, time bounds or part of day can be booked, whatever the agent decides
- Scheduler searches with the patient's filters; when nothing matches it says so instead of booking
- Regression tests replay the exact reply

## What went well
- Caught before any real patient contact
- Layered design made the fix local: one prompt version, one guard, no graph change

## Follow-ups
- Booking guard in front of the LLM scheduler agent (milestone 3 step 4)
- Safety reviewer checks the message against the patient's request (step 5)
- Add this reply to the eval regression suite (milestone 5)
