# 0007 Nurse handoff: honest callback times, and the AI steps back

Status: Accepted

## Context
- Some replies must reach a human: symptoms, medical questions, "can I talk to a nurse", repeated confusion,
  failed drafts, a failed identity check
- Telling a patient "a nurse will call you soon" when the queue is two days deep breaks trust and can be unsafe
- If the AI keeps messaging while a nurse is handling the case, the patient gets two voices and mixed answers
- The AI must never answer a clinical question, even when the model could

## Decision
- Escalation creates a nurse task through the cases MCP server; the kind and priority are decided by code
  (symptom and emergency: urgent, within the hour; questions and callbacks: routine)
- Routine tasks are due 5 PM on the first business day with capacity left (about 20 callbacks a day), skipping
  weekends; the patient's acknowledgement quotes that exact due time
- Acknowledgements are fixed templates in the patient's language; symptom acknowledgements go out as
  safety messages (not held for quiet hours) and always include the 911 safety net
- No reply for abusive messages or failed identity checks; outreach turns that fail send nothing
- The case is set to escalated and the graph skips it until a nurse resolves the task with hand_back=True
- A second model reviews every draft after the rule checks; medical advice is never rewritten, it goes to a nurse;
  if the reviewer is down the draft is sent (rules already passed) and flagged

## Consequences
- Every promise made to a patient matches a real queue entry, and it can be audited
- Nurse capacity is a real constraint the simulation (milestone 4) can stress
- Paused cases need a nurse action to resume; the nurse queue screen comes in milestone 4
