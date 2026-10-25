# Milestone 7: see what happened

Goal: anyone (a reviewer, a clinician, an interviewer) can open one patient's case and read what the system did, in order, without reading logs or SQL.

```bash
make outreach-setup && make simulate days=30   # a program to look at (rule-based agents, free)
make timeline                                  # http://localhost:8502
```

## 1. Case timeline

An audit trail is the ordered record of every action on a case and who took it. The timeline is that record, put in plain words on one screen.

What it shows, oldest first, grouped by day since discharge:
- Discharge, the risk tier and the model's top reasons (SHAP drivers), the barriers on file
- Every message: patient, AI and nurse, with channel, delivery status and quiet-hours holds
- What the AI decided: how it read each reply (intent), referrals filed, barriers and preferences saved, language and channel changes, identity checks
- Nurse handoffs (kind, priority, interpreter) and how the nurse resolved them
- Bookings, reschedules, cancellations, no-shows, attended visits
- Re-scores after discharge (landmark days, ED visits, missed fills) with a risk chart
- The outcome: readmitted, or the case closed and why

Shortcuts open one example of each story: readmitted, urgent nurse handoff, needed an interpreter, switched language, missed a visit, attended, risk rose after discharge, filed a referral, opted out, no reply.

Why it is built this way:
- Read only, and built from the tables the system already writes (cases, events, messages, nurse tasks). No extra logging for the demo, so the screen can't disagree with the audit trail
- The wording lives in `src/readmit/viewer/timeline.py` (tested, tests/test_timeline.py); `app.py` only draws it
- Inside one agent turn everything shares the simulation clock's minute, so items in the same minute are ordered the way a turn runs: what the patient wrote, what the AI decided and did, its message, the status it left
- Non-English messages can be machine translated, cached, labeled as such (the nurse screen's rule)

Limitations:
- Holdout patients (the randomized 20% with no outreach) have no case, so no timeline
- The LLM's reasoning isn't stored, only its decisions (the intent it read, the actions taken). Full prompts and outputs are in LangSmith when tracing is on
- Synthetic patients only; a real deployment would need access control and an access log on this screen, like any chart
