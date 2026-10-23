# 0009 Code owns the facts of a conversation; the reviewer blocks only on safety

Status: Accepted

## Context
- Conversation evals: a patient replied "the first one" a day later; the scheduler searched again, the list had changed, and it booked a different time. The reviewer caught it, the turn went to a nurse
- Offers were never stored: "the first one" was re-derived from the transcript by a model
- The LLM reviewer kept failing good drafts for polish ("option 3 doesn't name the doctor"); two failures sent the case to a nurse, and with the retry loop, Jose never got a first message
- Facts (times, dates, booking claims, contact details, diagnosis) are already checked by code before the reviewer

## Decision
- Every message that offers times stores them (outreach.offers: case, option number, slot) through the scheduling server
- A reply that picks a numbered option ("the second one", "la primera", "2") is resolved by code against the stored offer. Still open: booked. Gone: never swapped for another time; the patient is told and offered new ones
- The reviewer's safety checks (medical advice, private details, tone) always block. If the reviewer asks only for polish and the draft was already rewritten once, it is sent and the reasons are kept as advisory

## Consequences
- The patient books exactly what they saw, or hears plainly that it's gone
- Fewer LLM calls on the commonest reply
- Some sent messages carry advisory notes; they are reviewable, and a later judge (milestone 6) can score them
- A free-text pick that isn't a number ("the Thursday one") still goes through the scheduler agent
