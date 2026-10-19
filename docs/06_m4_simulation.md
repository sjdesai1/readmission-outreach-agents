# Milestone 4: The conversation loop over simulated days

Goal: run the whole program hour by hour, with scripted patients, real appointments, nurses, outcomes and costs, so every part from milestones 1 to 3 is exercised together and the effect on readmissions can be measured.

```
each simulated hour
  deliver held messages (quiet hours over)
  settle visits that ended: attended or no-show
  decide 30-day outcomes whose date has come
  7 AM: the clinic cancels a few visits
  patient replies that are due -> inbound turn
  nurses work the queue, hand cases back
  re-scores: ED visit, missed fill, day 7 / 14 / 21
  9 AM to 7 PM: day-before reminders, then one outreach turn per due case
  delivered messages reach the scripted patient (or bounce)
```

Run: `make outreach-setup && make simulate days=90` (rule-based agents, free). `llm=1` for the real agents, `cases=5` to cap turns per hour.

## 1. Cadence

- Attempt 1, a follow-up at +48h, a phone call at +96h if calls are allowed, then closed as unreachable
- Counted from the last hand-back or rebook, so a nurse note or a no-show restarts it
- Outreach 9 AM to 7 PM; replies any hour, but non-urgent messages at night are held until morning
- At most one AI message a day starts from outreach: a case already messaged today waits until tomorrow

## 2. Scripted patients

Persona per patient from the hidden truth table, seeded so reruns match.

| Persona | Behavior |
|---|---|
| engaged | answers, books |
| busy | answers late, asks for evenings |
| silent | never answers |
| med_question | asks a medication question (nurse) |
| symptomatic | reports a symptom (urgent nurse) |
| opt_out | replies STOP |
| no_ride | no transport (referral, video visit) |
| injection_attempt | tries prompt injection |
| abusive | abusive reply (nurse review) |
| spanish_speaker | replies in Spanish |

Some contacts are wrong on purpose: bounced emails and wrong numbers mark the channel bad and outreach moves on.

## 3. Appointment lifecycle

- Booked, reminded the day before, attended or no-show (seeded per persona)
- No-show or clinic cancellation: back to outreach to rebook, with a message that says why
- A completed visit closes the case as completed

## 4. Nurse queue screen

`make nurse`: Streamlit screen over the same store. Queue in order (emergencies, urgent, due time), full thread, reply as the nurse, resolve and hand back. The simulation has its own nurses: urgent any hour, routine on weekday shifts, 3 an hour.

## 5. Outcomes and the holdout

- Each flagged stay gets both potential outcomes from one random draw: readmitted with usual care, or after a completed visit (risk cut by 25 to 60 percent, more for heart failure, COPD, no ride)
- Scoring already put 20 percent of flagged patients in a holdout with no outreach: a randomized comparison
- The report card shows three numbers side by side

90 days, demo world:

| Comparison | Readmitted | Why it's there |
|---|---|---|
| outreach vs holdout | 21% of 42 vs 40% of 10 | randomized: unbiased, but 10 patients is noise |
| attended a visit vs not | 19% of 21 vs 24% of 21 | self-selected: biased |
| true expected effect | 3.8 points fewer | known only because the world is simulated |

About 3,500 patients per arm would detect an effect this size. Many-seed confidence intervals come in milestone 5.

## 6. Re-scoring after discharge

See [05_m4_rescoring.md](05_m4_rescoring.md): event-triggered re-scores, the logistic update of the discharge risk, and the outreach-signal study on a 20x simulated program with a positive control.

## 7. Cost controls

| Control | What it does | Why |
|---|---|---|
| Daily spend cap | once today's real LLM calls cost `LLM_DAILY_BUDGET_USD` (default $1), new calls are refused; turns run on the rule-based agents | a bug or a loop can't run up a bill; patients still get messages |
| Cache hits still served | identical requests answered from cache even over budget | reruns stay free |
| Over budget is not an escalation | the reply interpreter falls back to rules instead of paging a nurse | a cost problem must not become a nurse workload problem |
| One message a day | a case messaged today isn't outreach-due again until tomorrow | fewer tokens, and no patient gets nagged |
| Run summary | `make simulate llm=1` prints calls, cache hits, failures, tokens, cost, budget left | cost is visible per run, not discovered on the invoice |

See [ADR 0008](decisions/0008-spend-cap-falls-back-to-rules.md).

## 8. What broke and what changed

- Paused cases skipped the input guard: "chest pain" while waiting for a nurse got no 911 message ([postmortem](postmortems/2026-10-15-paused-case-emergency.md))
- Setup left old nurse tasks behind; a replayed idempotency key returned a stale task ([postmortem](postmortems/2026-10-15-stale-nurse-task.md))
- Random appointment ids made attendance differ between runs: seeded by case and start time instead
- First landmark models overfit; a fill signal meant different things on different days ([05](05_m4_rescoring.md))
