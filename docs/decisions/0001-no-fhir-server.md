# 0001 No FHIR server

Status: Accepted

## Context
- The project is about agents, MCP and evals, not EHR integration
- A local FHIR server (HAPI) adds a heavy container, a data loader and a second data model
- Agents only need a small, minimum-necessary patient summary

## Decision
- Patient data comes from Postgres through the patient_data MCP server
- No FHIR server in the stack

## Consequences
- Faster to build and run; one source of truth
- In production, only the patient_data server changes: it calls the hospital FHIR API instead of Postgres
- Agents, prompts and tests stay the same, since they only see the MCP tool contract
