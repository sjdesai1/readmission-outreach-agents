# 0008 Spend cap: over budget, the rule-based agents take over

Status: Accepted

## Context
- Every agent turn can make several LLM calls; a loop or a bug can multiply them quickly
- The project must stay free; a real program has a monthly budget
- Stopping outreach when money runs out would leave patients without follow-up
- Each LLM agent already has a rule-based version (the stubs), used in tests and when a call fails

## Decision
- `LLM_DAILY_BUDGET_USD` (default $1): real calls logged in `ops.llm_calls` today are summed before each new call
- Over the cap, `complete_json` raises `LLMBudgetExceeded` without calling the API; cached answers still return
- `run_turn` checks the cap first and runs the turn on the rule-based agents; a notice prints once
- The reply interpreter treats over-budget as "read with rules", not as "unreadable, send to a nurse"
- Outreach starts at most one AI message per case per day

## Consequences
- Spend is bounded per day; quality drops to templates, safety rules are unchanged (they never used the LLM)
- Template messages are blander; the run summary and `ops.llm_calls` show when the cap was hit
- The cap is per database day, not per patient or per run; a per-run cap can come later if needed
