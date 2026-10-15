# 0003 Business rules behind MCP

Status: Accepted

## Context
- A rule stated only in a prompt can be ignored, misread or jailbroken
- Several callers act on the same data: agents, the sequencer, tests, a future nurse UI

## Decision
- Every rule lives in the outreach store, reached only through MCP servers: consent, quiet hours, atomic booking,
  idempotency, case open and closed checks, identity lockout, channel changes
- Each agent role gets a scoped gateway with only the tools its job needs
- Rule violations return readable tool errors with the next step ("use reschedule_appointment instead")

## Consequences
- Defense in depth: prompts describe the rules, servers enforce them
- One place to test rules (store tests) independent of any LLM
- A misbehaving agent can't break a rule, only trigger a refusal that the graph handles
