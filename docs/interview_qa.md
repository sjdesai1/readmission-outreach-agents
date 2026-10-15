# Interview Q&A

One section per milestone. Answers written the way to say them.

## Milestone 0: Local stack

**Q: Walk me through your local setup.**
Docker Compose runs the full stack: Postgres, Airflow, MLflow and Mailpit to catch outgoing email. Gemini and LangSmith are the two cloud pieces. One command brings it up, and a smoke test checks each service from the laptop and from inside Airflow.

**Q: Airflow and your ML libraries have conflicting dependencies. How did you handle it?**
Airflow only orchestrates. Project code runs in a separate virtual environment inside the image, and tasks call that Python directly. Airflow's pinned versions never touch XGBoost or LangGraph. In production the same idea is one container per task, for example with the KubernetesPodOperator.

**Q: How does the same code run on your laptop and inside Docker?**
All settings come from environment variables. On the laptop, `.env` points at localhost ports. Inside Docker, compose overrides them with service names. The config module reads whatever is set, so there's no environment-specific code.

**Q: How do you handle secrets?**
Keys only live in `.env`, which is gitignored. `.env.example` is committed with every variable and blank secrets. A unit test fails if the code reads a variable that isn't documented there. In production they'd come from a secrets manager like Vault or AWS Secrets Manager.

**Q: Why Postgres for both Airflow and the app?**
One server, separate databases: Airflow metadata, MLflow tracking, and app data. Simple locally, and isolated enough that a reset of one doesn't affect the others. In production these would usually be separate managed instances.

**Q: What does MLflow store where?**
Run metadata, params, metrics and the model registry go to Postgres. Model files and plots go to the artifact store, here a Docker volume, in production S3 or similar.

**Q: What does your smoke test check, and why bother?**
Each service independently: Postgres query, an MLflow run with an artifact, an email sent and found in Mailpit, Airflow's health endpoint, Gemini model listing and a call through LiteLLM with fallback, and a LangSmith trace. It runs from the laptop and as an Airflow DAG. It catches broken wiring before any real pipeline runs, and it's the first thing I run after any infra change.

**Q: What would change for production?**
Managed Airflow with components split out, managed Postgres, S3 for artifacts, a BAA-covered LLM provider behind the same LiteLLM interface, secrets from a vault, LangSmith self-hosted or with PHI redaction, and Kubernetes instead of Compose.

## Milestone 1: Simulated hospital, extraction, risk model

**Q: How did you build the data, and why synthetic?**
Synthetic hospital with a hidden risk formula: patients, stays, messy notes, consent. Real patient data is PHI. Synthetic also gives a known truth, so every component can be scored exactly, including a ceiling for the model.

**Q: How do you define the label?**
Unplanned admission of the same patient within 30 days of discharge. Planned procedures don't count. Labels are computed before exclusions, because a stay that ended in death can still be someone else's readmission. Each rule has a unit test.

**Q: How did you prevent leakage?**
Time-based split, never random. Prediction time is discharge + 24h, and only notes written by then are used. The data includes a post-discharge call note that reveals the outcome; the cutoff removes it. Labels only count once 30 days have passed, so August discharges are excluded from evaluation.

**Q: Why use an LLM on the notes?**
Social barriers like living alone or no transportation only live in free text. The LLM reads them in batches with a strict JSON schema, and every finding must quote the notes exactly or it's dropped. That grounding check is the defense against hallucination.

**Q: How did you evaluate extraction?**
Precision, recall and F1 per factor against an answer key of what was actually written before the cutoff. 0.993 F1. When one factor lagged, error analysis showed the label was wrong, not the model, so I fixed the data instead of tuning the prompt to match a bad label.

**Q: What does your LLM client do?**
One client for the whole project: fallback model, retries with backoff, rate limiting, a response cache, schema validation with one repair retry, LangSmith tracing, and a log of tokens, cost and latency for every call. Tested with a fake model, so no API calls in CI.

**Q: Did the LLM features improve the model?**
No, and I tested it rather than assuming. Lift was +0.003 AUROC with a bootstrap 95% CI crossing zero. The true factors didn't help either, which points to training size, not extraction quality. The features still matter operationally: the agents use the barriers to plan outreach.

**Q: Why XGBoost, and how did you justify it?**
Compared against LACE, the clinical standard (0.714), and logistic regression (0.768). XGBoost reached 0.804 against a 0.843 ceiling. Reported by new vs repeat patient, with calibration by quintile.

**Q: How does the care team use the scores?**
Fixed cut points from the test period: high = top 10%, medium = next 20%. Calling the top 30% reaches about two thirds of eventual readmissions. Everyone else still gets a check-in, because the model misses about a third. Each patient comes with top SHAP reasons and extracted barriers.

**Q: How is the model deployed?**
Registered in MLflow as readmission-risk with a champion alias. The scorer loads by alias, so promoting a new version is an alias change, no code change. Tier cut points and the feature list are stored next to the scores.

**Q: How did you set up the holdout?**
20% of flagged patients get standard care only, chosen by a hash of the patient id: deterministic, reproducible, not tied to risk. Comparing readmissions between groups is the only way to claim the outreach caused a reduction.

**Q: Tell me about a bug you debugged.**
A commit silently left files out. git check-ignore showed the pattern data/ in .gitignore matched src/readmit/data/ too. Anchored it to /data/. Found it because the commit summary said 1 file changed when I expected 3.

**Q: Why did you drop the FHIR server?**
The project is about the agents and evals, not EHR plumbing. Agents reach patient data through MCP servers backed by Postgres. In production, that one MCP server would call the hospital's FHIR API instead; nothing else changes.

## Milestone 2: MCP servers

**Q: What is MCP and why use it?**
A standard protocol for giving models tools, resources and prompts. Any MCP client can discover and call the tools, so the agents aren't tied to one framework. Swapping the backend (Postgres today, FHIR in production) changes one server, not the agents.

**Q: How did you split the servers?**
Five, by business capability: patient data, scheduling, messaging, referrals, cases. Least privilege: an agent connects only to what its job needs. The message writer can't book; the scheduler can't message.

**Q: Where do the business rules live?**
In the store behind the servers, not in prompts. Consent, minimum necessary, atomic booking, idempotency, case open and closed checks. A prompt can be ignored or jailbroken; a server check can't. Defense in depth: the agent is told the rule and the server enforces it.

**Q: How do you stop double booking and duplicate messages?**
Idempotency keys on every write, unique in the database. A retry with the same key returns the original result. Booking locks the slot row and allows one active appointment per patient. The key is checked before the rules, so a retry after a timeout learns what actually happened.

**Q: How does an agent learn it did something wrong?**
Rule violations become MCP tool errors with the reason and the next step, like "already has appointment, use reschedule_appointment instead". The agent can read it and correct course instead of crashing.

**Q: How do you keep PHI away from the model?**
Minimum necessary. The summary tool returns first name, age, language, allowed channels and risk reasons. Never phone, email or last name. The messaging server looks up the address at send time, so the model sends to a case id it never resolves.

**Q: How did you test the servers?**
Store rules against a real Postgres, MCP tools through an in-memory client on the real protocol path, and CI with a Postgres service container. Then by hand in MCP Inspector.

**Q: Tell me about a bug you found here.**
In the Inspector I booked a visit dated a week before the patient's discharge. The sim clock was Sep 1; the case opened Sep 9. The server checked the slot and consent but not whether the case was open. Added the check to every write, wrote a test reproducing the exact call, confirmed the refusal live.

**Q: Why HTTP instead of stdio?**
Each server runs in its own container, like separate services owned by separate teams. Streamable HTTP lets any agent process reach them over the network. stdio is still available with --stdio for local debugging.


## Milestone 3: Agents

**Q: Is this a multi-agent system with an orchestrator?**
No orchestrator LLM. A LangGraph workflow with deterministic routing: code controls the flow, LLMs do the judgment. Five agents (interpreter, planner, scheduler, writer, reviewer) are nodes; guards, identity, sending and escalation are rule nodes. Every path is testable and explainable.

**Q: Which parts use an LLM, and why those?**
Reading free-text replies, planning the approach, choosing slots from messy requests, writing the message, reviewing it. Everything with one right answer stays in code: STOP, emergencies, consent, identity, booking rules, who gets a nurse and how soon.

**Q: How do you stop the scheduler agent doing something wrong?**
It picks one action per step as validated JSON. Code adds the case id and idempotency keys, so it can't act for another patient or double book. It can only book slots that find_slots returned this turn, and only if they fit what the patient asked for. Max 5 steps, rule-based fallback.

**Q: What does the safety reviewer add over rule checks?**
Rules catch invented times, contact details, diagnosis words, tone. The reviewer catches what rules can't: hidden medical advice, a draft that ignores the patient, re-asking a question already answered. Its own checklist overrules its verdict; medical advice goes straight to a nurse; anything else gets one rewrite. If it's down, the draft still goes out after the rule checks, flagged.

**Q: How do you handle a patient who needs a human?**
A nurse task with a due time from real capacity: emergencies now, symptoms within the hour, questions by 5 PM the next business day with room. The patient is told that exact time. The AI pauses on the case until the nurse hands it back, but STOP and emergencies are still handled.

**Q: Tell me about a bug you found in the agents.**
Paused cases skipped every turn, so "chest pain" while waiting for a nurse got no 911 message. I'd treated paused like closed. I found it by testing the riskiest edge on purpose, predicted it before running, fixed it by sending replies on paused cases through the input guard, and wrote a postmortem.

**Q: How did you debug agent behavior?**
LangSmith traces per turn. The reviewer was rewriting most drafts; the trace showed why: the writer's phone style forced a closing question, so confirmations asked "does this time still work?" Fixed the writer prompt, not the reviewer.

**Q: How do you keep PHI out of prompts and traces?**
The name is a {first_name} token until the send node; contacts never leave the server; traces mask contacts; the diagnosis is blocked from messages outside the logged-in portal.

**Q: What does memory remember, and what doesn't it?**
Only standing preferences from a fixed list (part of day, time bounds, days, visit type), stated by the patient as ongoing. Never symptoms, never one-off requests. Days and time of day combine with each new request.
