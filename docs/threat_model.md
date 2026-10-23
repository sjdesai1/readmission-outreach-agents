# Threat model

Scope: the outreach system. Patients' messages come in on SMS, email, portal and phone; agents decide and act through MCP servers over the case store. The risk model (milestone 1) is out of scope here.

Tested by `make eval-redteam` (evals/redteam.jsonl): every attack below is a case, tagged with the defense expected to stop it. Results in [07_m5_evals.md](07_m5_evals.md).

## What needs protecting

| Asset | Why it matters |
|---|---|
| Patient health information (diagnosis, stay, medications) | HIPAA; a phone or inbox is often shared |
| Contact details (phone, email) | identity theft, harassment |
| Appointments and clinic slots | a booked or cancelled visit is a real-world action |
| Clinical safety | a symptom must reach a nurse; the AI must never give medical advice |
| Consent | STOP must stop everything; only consented channels are used |
| Nurse time | false escalations and loops starve real patients |
| LLM budget | runaway calls cost money |

## Who might attack, and how

| Actor | Entry point | Goal |
|---|---|---|
| A patient testing limits | any channel | medical answers, jailbreaks, extra slots |
| Someone with the patient's phone or inbox (family, partner) | SMS, email | the patient's health information, cancel a visit |
| Someone pretending to be staff ("this is Dr. Smith") | any channel | health information |
| An attacker who knows a phone number | phone, SMS | book or cancel as the patient |
| Text crafted to steer the model (prompt injection) | any free-text reply | other patients' data, actions, prompt contents |
| Accidental: very long or odd messages | any | cost, crashes, garbage replies |

Out of scope: compromised servers or database, insiders, the LLM provider itself (covered by a BAA and contracts in a real deployment).

## Threats, defenses, tests

OWASP Top 10 for LLM applications (2025) where it applies.

| Threat | Defense (layer) | Red-team cases |
|---|---|---|
| LLM01 prompt injection: "ignore instructions", admin mode, role play, Spanish | The model never sees other patients or contact details (pseudonymization); slots, bookings and consent are enforced in the store, not in prompts (code-owned facts); the patient's text is marked as data in every prompt | inj-* |
| LLM02 sensitive information disclosure: diagnosis by SMS, contacts read back | Diagnosis blocked outside the logged-in portal (output checks); contacts never leave the server (pseudonymization) | phi-* |
| Impersonation: family member, "Dr. Smith", "I'm her son, cancel" | Third-party detection sends a fixed privacy reply (input guard); cancelling needs the patient | imp-* |
| Identity on the phone: skip the check, wrong birth year | Year of birth verified server-side before anything else; 3 failures go to a nurse (identity check) | imp-phone-* |
| LLM05 improper output handling: links, phone numbers, shouting, pressure | Output checks reject contact details, placeholders, ALL CAPS, pressure words, invented times and dates | inj-link, inj-phone-number, inj-shouting |
| LLM06 excessive agency: book every slot, someone else's slot, a past or invented time | Each agent role reaches only its own tools (MCP permissions); one upcoming visit per case; only slots the store returned can be booked; offers stored, picks resolved by code (code-owned facts) | agency-* |
| LLM07 system prompt leakage | Prompts hold no secrets; the reviewer and output checks see every draft; tool lists go only to the scheduler | leak-* |
| LLM09 misinformation (medical advice): double dose, "is it normal", pharmacist said | Clinical questions go to a nurse with an honest callback time; the reviewer escalates any medical advice (nurse handoff) | med-* |
| Missed emergency: "chest pain but don't call", Spanish breathlessness, emergency while paused | Emergency patterns checked before any agent, on every reply, even on paused cases (input guard) | safety-* |
| Consent: STOP variants, a false STOP | Exact STOP words handled by rules before any agent; "don't stop" is read by the interpreter | optout-* |
| Abuse and threats | Review task for a nurse, no AI reply | abuse-* |
| LLM10 unbounded consumption: huge messages, essay requests | Channel length limits (output checks); daily spend cap falls back to rules (ADR 0008); one outreach message a day | dos-* |

## Residual risks

- Pattern lists (emergencies, STOP, third party) miss phrasings no one wrote down; the LLM interpreter is the second net, the rule-based fallback is not
- A family member who answers the year of birth correctly is treated as the patient
- The reviewer is a model: it can miss subtle advice; it only blocks, never adds content
- Email and SMS are not secure channels; only the portal may carry clinical details
- Simulated data only: no real PHI anywhere in this repo
