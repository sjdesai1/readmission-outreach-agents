# 0013 An LLM outage falls back to rules, like the spend cap

Status: Accepted. Extends [0008](0008-spend-cap-falls-back-to-rules.md)

## Context
- ADR 0008: over the daily budget, the rule-based agents run the turn. Every agent also falls back to its rule-based version when a call fails, except the reply interpreter, which sent the reply to a nurse ("could not read the reply")
- A free-tier daily quota ran out during a conversation eval run: every patient reply after that became a nurse task. 11 of 22 conversations ended closed by the circuit breaker; abusive patients were handed back and messaged again
- The safety rules (STOP, emergencies, third parties, identity, languages) never use the LLM, so they still run during an outage

## Decision
- Any LLM error while reading a reply (rate limit, timeout, both models failing, over budget) reads it with the rule-based interpreter. The intent is marked `read_by: rules` with the error, for the dashboard and traces
- Nurse handoffs stay where they belong: symptoms, clinical questions, requests for a person, abuse, two unclear replies in a row, a draft that fails twice

## Consequences
- An outage lowers reply quality to the rule-based level (52 of 57 turn cases) instead of flooding the nurse queue
- Nuanced replies the rules can't read (an indirect barrier, "stop contacting me") get the "didn't understand" reply during an outage; the second unclear reply still reaches a nurse
- Outages are visible: `ops.llm_calls` failures on the dashboard, `read_by: rules` on each turn
