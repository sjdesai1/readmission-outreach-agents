# 0002 Deterministic routing

Status: Accepted

## Context
- Two common designs: a supervisor LLM that picks the next agent, or a graph whose edges are decided by code
- Some paths must hold every time: STOP closes the case, emergencies reach a nurse, every message passes checks
- Tests need exact, repeatable paths

## Decision
- LangGraph workflow: routing functions in code read the state and pick the next node
- Agents write into the state (intent, plan, draft, verdict); they never choose the route
- Real autonomy only where it pays off: the scheduler's tool-calling loop, bounded by server rules

## Consequences
- Safety paths are guaranteed and testable (tests assert exact node paths)
- No extra LLM call per hop just to decide what's next: lower cost and latency
- New behavior needs a code change to the graph, not just a prompt change; acceptable for a regulated workflow
