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

## Milestone 4: Re-scoring after discharge

**Q: The discharge score is one snapshot. What happens after?**
Re-scores as information arrives. An ED visit or a missed prescription fill triggers one right away, with a nurse callback if the risk jumps. Days 7, 14 and 21 are a safety net.

**Q: Why a logistic update instead of retraining the model with the new signals?**
I tried the full refit first. After day 7 only 139 readmissions were left, and XGBoost overfit: the new signals made AUROC worse. Updating the discharge risk with 4 signals needs 6 numbers. Day 7 went from -0.007 to +0.043, CI excluding zero.

**Q: How did you pick the alert threshold?**
From the data. An ED visit multiplies the odds by 12 or more, so it alerts. A missed fill is about 1.2x, so it re-scores but rarely pages a nurse.

**Q: Did outreach behavior help predict readmission?**
Not yet. It has no history before the program, so I simulated a program 20x the demo. On the main world: null, because personas there are independent of risk. A simulation only shows what it encodes, so I ran a positive control where silent patients carry real extra risk. The pipeline found it (odds ratio 2.5 vs a true 2.2) and captured most of the possible gain, but the possible gain was one AUROC point: not significant at 1,000 cases. It stays out of the live model until a pre-specified test passes.

**Q: What's a positive control and why run one?**
Plant an effect you know, check the method finds it. Without it, a null result can't tell "no effect" from "broken pipeline". Mine also exposed an underpowered test and the cost of testing 6 signals instead of 2.

**Q: Tell me about a bug in the re-scoring.**
Days-to-fill, censored at the landmark, meant different things on different days. A day-7 model used on day 3 read a missed fill as good news, so no alerts ever fired. A summary table by trigger showed it: missed fills lowered risk. Fixed with yes/no signals and a day-3 model.

## Milestone 4: Simulation and cost

**Q: How do you test a conversational agent end to end without real patients?**
An hourly simulation: scripted personas reply (engaged, silent, symptomatic, injection attempts, Spanish), visits are attended or missed, the clinic cancels, nurses work the queue, outcomes are drawn at day 30. Seeded, so a run is reproducible and a bug can be replayed.

**Q: How do you know outreach reduces readmissions?**
Scoring holds out a random 20 percent of flagged patients. Outreach vs holdout is the honest comparison. Attended vs not attended looks better but is self-selected. In the simulation both potential outcomes are known, so each estimate can be graded against the truth. At this size the holdout is too small; about 3,500 per arm would detect the effect.

**Q: How do you control LLM cost?**
A daily spend cap checked before every call. Over it, the API isn't called and the turn runs on rule-based agents, so patients still get messages and safety rules are untouched. Cached answers stay free. At most one outreach message per patient per day. Every run prints calls, tokens and cost.

**Q: Why fall back to rules instead of stopping?**
Stopping leaves patients without follow-up, a clinical risk. Escalating everything to nurses turns a cost problem into a staffing problem. The rule-based agents already exist for tests, so degraded mode is tested every CI run.

## Milestone 5: Evals, red team, effect, A/B, monitoring

**Q: How do you evaluate an agent system?**
Three levels. Single turns check each decision (41 cases). Whole conversations over simulated days check that decisions add up (20 personas). A red team checks attacks (31, mapped to OWASP's LLM Top 10). Each level found bugs the others couldn't.

**Q: Give an example of a bug only the conversation evals found.**
Spanish messages offering a Tuesday slot never sent: "mar 15 sep" (martes) was read as March 15 by the date check. The draft failed, went to a nurse, came back, failed again: up to 21 nurse tasks per conversation. Single-turn cases never had a Tuesday slot. Fixed the date check, added a circuit breaker after three failures, wrote a postmortem.

**Q: What did the red team find?**
The LLM path held on all 31 attacks. The rule-based fallback, which runs when the LLM budget is spent, booked a visit for a Spanish-speaking patient reporting shortness of breath instead of sending 911. Spanish breathlessness phrases were added to the input guard, which runs before any agent.

**Q: Does the outreach work?**
In the simulation, yes: 5 points fewer readmissions. But a real program this size would see a non-significant result half the time; it needs about 1,234 patients per arm and the 20% holdout has 286. The naive attended-vs-not comparison reported 14 points and was wrong half the time, because attenders were healthier.

**Q: How do you decide whether a prompt change ships?**
A paired A/B on the same 72 cases, with the hypothesis and the primary metric fixed before running. Primary: no case lost (McNemar). Secondary: length, reading grade, rewrites. Writer v11: no case lost, 7% shorter (CI excluding zero), fewer rewrites as a trend. Shipped.

**Q: What do you monitor?**
LLM spend against the daily cap, failures and latency by task, the nurse queue and overdue promises, and cases the AI keeps failing on. Two alerts go to email with runbook links. On its first live evaluation the failing-case alert found 40 cases left by a run before the fixes.

**Q: How do you handle patients who don't speak English?**
Language is a stored fact, decided by code. An explicit request ("text me in Spanish") switches it, with an audit event, and the times are offered again in Spanish. A reply in Spanish from an English-record patient gets one question, asked once: bilingual patients mix languages, and a silent switch could leave them unable to read us. Any language outside English and Spanish goes to a nurse with an interpreter, because nobody has evaluated the model's messages in it. Spanish STOP words are rules, like English ones.

**Q: Why not just let the LLM write in whatever language the patient uses?**
It probably would, fluently. But an outreach message carries times, dates and a 911 line; the output checks read English and Spanish dates only, and the evals cover only those two. An unchecked message in Vietnamese is a clinical risk I can't measure. Adding a language is templates, date checks and eval cases first, then a config change.

**Q: Why evaluate the rule-based fallback at all?**
It's what patients get when the budget runs out or the LLM is down. The red team showed the fallback had the only real safety gap.

## Milestone 6: LLM judge

**Q: How do you evaluate free-text output at scale?**
An LLM judge: a different model grades each message on seven yes/no criteria, a reason before each verdict. Yes/no, not 1-10: scores drift and can't be checked against a person.

**Q: How do you know the judge is right?**
Two checks it wasn't tuned on. Planted errors: good messages broken on purpose, one flaw each; it caught every wrong time, medical advice, pressure and jargon, with no false alarms, but only half the grammar and Spanish formality errors. And 20 messages I labeled blind: 18 of 20 agree, kappa 0.80.

**Q: Where does the judge fail?**
It can't verify promises. Both disagreements were "a nurse will follow up" with no nurse task behind it; the text looks fine. So promises are checked by code against what's on file, and Spanish formality by a rule. The judge grades wording; code checks facts.

**Q: Did you change the judge after seeing its mistakes?**
Once: it couldn't see the offer "the second one" referred to, and it failed correct refusals. That moved its pass rate from 90% to 99%, so those numbers are inflated by design; the trustworthy ones come from the planted set and my labels.

**Q: What did labeling find that the evals missed?**
Four real bugs: a template promising a nurse callback no one was assigned, the writer promising a free ride and an interpreter nothing arranged, video offered to a patient who can't do video, and leg swelling inside a yes booked instead of sent to a nurse in the fallback.

**Q: Did the prompt A/B hold up?**
No. The milestone 5 result, v11 three words shorter, didn't replicate on a rerun with more cases. v11 stayed because it loses nothing. A small secondary effect from one run is a hypothesis.
