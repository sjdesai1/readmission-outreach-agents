# 0011 The judge grades wording; code checks promises and formality

Status: Accepted

## Context
- The LLM judge (milestone 6) agrees with a person on 18 of 20 messages (kappa 0.80) and catches every planted wrong time, medical advice, pressure and jargon
- It misses half the planted grammar and Spanish formality errors ("you visit are set", "tu visita")
- Both disagreements with the person were messages promising "a nurse will follow up" with no nurse task behind them. The judge reads text; it can't see the case
- Labeling also found the writer promising a free ride and an interpreter that nothing arranged

## Decision
- The judge's scores are used for what it measures well: tone, advice, pressure, jargon, answering the patient, facts given to it. Reported per criterion, never as one number alone
- Promises are checked by code before a message is sent: a nurse callback needs a nurse task, a ride needs a transport referral, an interpreter needs an interpreter referral, video needs telehealth consent. A draft that promises what isn't on file goes back to the writer, like a wrong time does today
- Spanish formality is checked by code: tú forms in a Spanish message fail the output checks
- Every judge change is measured on the planted set and the labels, not on the runs it was tuned on

## Consequences
- The judge's weak criteria stop being trusted silently
- The output checks grow a promise check and a formality check (next PR), so the fallback path gets them too
- Labels need refreshing when the writer or the judge changes enough; 20 more take about 15 minutes
