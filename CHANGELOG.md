# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org/);
each minor version is a finished milestone.

## [Unreleased]

### Added
- Milestone 3 (in progress): LangGraph turn graph with rule nodes and an MCP gateway with per-role tool access
- Reply interpreter, care planner and message writer agents with versioned prompts and Pydantic schemas
- Guardrails: STOP keywords, emergencies, family-member replies, injection flags, invented-time and tone checks,
  no diagnosis outside the patient portal, booking guard that never books a time the patient ruled out
- Privacy: the LLM never sees the patient's name; contacts masked in prompts and traces
- Server rules: quiet hours, provider matching (follow-up deadline, language, own doctor), channel changes,
  identity check by year of birth on the phone
- Repo hygiene: pre-commit (ruff, gitleaks), PR and issue templates, license, coverage, type check and
  dependency audit in CI, Dependabot

### Fixed
- Agents booked a time the patient had ruled out ("can't do noon" booked noon)
- Actions were possible on a case before it opened (booking dated before discharge)

## [0.2.0] - Milestone 2

### Added
- Outreach system of record with business rules (consent, atomic booking, idempotency, audit)
- Five MCP servers: patient data, scheduling, messaging, referrals, cases

## [0.1.0] - Milestones 0 and 1

### Added
- Local stack: Postgres, Airflow, MLflow, Mailpit; smoke tests; CI
- Simulated hospital with messy notes, LLM extraction of social factors, XGBoost risk model, live scoring
