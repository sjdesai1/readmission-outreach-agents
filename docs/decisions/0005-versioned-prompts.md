# 0005 Versioned prompts

Status: Accepted

## Context
- Prompt changes change behavior as much as code changes
- Evals need to compare versions; logs need to say which version produced each output

## Decision
- Each prompt is a file named name_vN.md; a change creates a new file, the old one stays
- llm_agents.PROMPTS maps each task to its active version
- Every LLM call logs task and prompt version (ops.llm_calls, LangSmith metadata)

## Consequences
- Any output can be traced back to the exact prompt text
- A/B comparisons and rollbacks are a one-line change
- The response cache keys on the full prompt, so a new version never reuses an old answer
