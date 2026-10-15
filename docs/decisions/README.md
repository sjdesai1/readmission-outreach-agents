# Architecture decision records

One file per significant decision: context, decision, consequences. Never edited after acceptance;
a changed decision gets a new record that supersedes the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-no-fhir-server.md) | No FHIR server; patient data through an MCP server over Postgres | Accepted |
| [0002](0002-deterministic-routing.md) | Code routes the graph; LLMs make judgments inside nodes | Accepted |
| [0003](0003-rules-behind-mcp.md) | Business rules live in the store behind MCP, not in prompts | Accepted |
| [0004](0004-llm-never-sees-names.md) | The LLM never sees the patient's name or contact details | Accepted |
| [0005](0005-versioned-prompts.md) | Prompts are versioned files, never edited in place | Accepted |
| [0006](0006-scheduler-agent-loop.md) | Scheduler agent: structured action loop over MCP, guarded by code | Accepted |
