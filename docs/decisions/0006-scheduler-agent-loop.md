# 0006 Scheduler agent: structured action loop over MCP

Status: Accepted

## Context
- Scheduling replies vary too much for fixed branches: picked options, new constraints, reschedules, taken slots
- Native function calling ties the loop to one provider's tool format and bypasses the shared client
  (cache, schema validation and repair, cost log, tracing)
- The agent must never act for another patient, invent a slot, or book a time the patient ruled out

## Decision
- One action per step as validated JSON (call_tool / offer / give_up) through the shared LLM client; max 5 steps
- Tools discovered at runtime from the scheduling MCP server, filtered to the scheduler's role
- case_id and idempotency keys removed from the schemas the model sees; code injects them
- Code checks before every booking: slot returned by find_slots in this turn, and slot_fits the request
- First outreach uses the deterministic path (no LLM); the agent runs only for replies that need judgment

## Consequences
- Every step is logged, cached, validated and traced like any other LLM call
- Guardrails hold whatever the model decides; blocked actions return errors it can recover from
- Switching to native function calling later only changes how an action is requested, not the guards
