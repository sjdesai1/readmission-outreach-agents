# Milestone 2: Outreach system of record + MCP servers

Goal: give the agents a safe, narrow set of doors into the outreach system. Every business rule lives behind those doors, so no agent (or bug in an agent) can break them.

```
agents (milestone 3)  ->  5 MCP servers (HTTP)  ->  outreach store (rules)  ->  Postgres (outreach schema)
```

Run it: `make outreach-setup` (reset calendar, clock, cases), `make up` (servers), `make inspector` (call tools by hand).

## 1. Outreach store (`src/readmit/outreach/`)

| File | What it does |
|---|---|
| `schema.sql` | Tables: sim_clock, providers, slots, cases, events, messages, appointments, referrals |
| `setup.py` | 3 providers, 30-min weekday slots (70% already taken), clock at Sep 1, one case per flagged non-holdout patient |
| `store.py` | Every action and every rule; the only code that writes outreach tables |

Rules enforced in the store
- Minimum necessary: summaries carry first name, age, language, allowed channels, risk reasons; never phone, email, last name
- Consent: messages only on channels the patient opted into; telehealth only if opted in and has a device
- Case must be open: no booking, message, referral or status change before discharge + 24h on the sim clock
- Closed cases: no messages or bookings after opted_out, completed, readmitted
- Atomic booking: slot row locked (`for update`), one active appointment per patient, 2h minimum lead time
- Idempotency: every write takes a key; a retry returns the original result, never a duplicate
- Idempotency checked before rules: a retry reports what already happened, even if rules would now refuse it
- Audit: every action writes an event (who, what, when on the sim clock)
- Simulation clock: one stored "now", so simulated days run in seconds
- Contact details stay server-side: the store looks up the email address at send time; Mailpit receives it

## 2. MCP servers (`src/readmit/mcp/`)

One container each, streamable HTTP, least privilege: an agent only connects to the servers its job needs.

| Server | Port | Tools | Resources |
|---|---|---|---|
| patient_data | 8101 | get_patient_summary | patient://{case_id}/summary |
| scheduling | 8102 | get_current_time, find_slots, book_appointment, reschedule_appointment, cancel_appointment | |
| messaging | 8103 | send_message, get_thread | |
| referrals | 8104 | create_referral | |
| cases | 8105 | get_case, update_case_status, add_case_note | |

Design choices
- Typed parameters: `Literal` enums and `Field` descriptions; bad values rejected before any rule runs
- Tool annotations: readOnlyHint, idempotentHint, destructiveHint (cancel), so clients can gate risky calls
- Server instructions: tell the model how to use the tools (find_slots before booking, never invent times)
- Errors written for agents: a broken rule becomes a `ToolError` with the reason and the next step ("use reschedule_appointment instead")
- `common.py`: ports, annotations, StoreError to ToolError mapping, HTTP or `--stdio`
- Compose: one shared `x-mcp` block; only one service builds the image, the other four reuse it

## 3. Tests

- `test_outreach_store.py` (9): minimum necessary, slot filtering, idempotent booking, double booking, telehealth consent, reschedule frees slot, message consent + idempotency, closed case, audit trail, no actions before a case opens
- `test_mcp.py` (5): each server exposes only its own tools, summary and booking through MCP, rule violation arrives as a readable error, schema rejects invalid values
- In-memory fastmcp client: tests the real protocol path without network
- Real Postgres (`app_test` database); CI runs it with a Postgres service container
- 40 tests total

## 4. Manual verification (MCP Inspector)

- find_slots, book_appointment: booked a telehealth slot with a video link
- Same key again: same appointment, "already booked" note
- New key: refused, "already has appointment ... use reschedule_appointment"
- get_patient_summary: no contact details returned
- send_message: email delivered to Mailpit at an address the caller never saw
- Bug found: booking dated a week before discharge was accepted. Case open time wasn't checked. Fixed in the store, regression test added, verified the refusal in the Inspector
- Browser GET on `/mcp` returns "Client must accept text/event-stream": the endpoint speaks MCP, not HTML

## 5. What production would change

- patient_data server calls the hospital FHIR API instead of Postgres
- Auth on every server (OAuth per the MCP spec), per-agent scopes
- SMS and phone through a real provider (Twilio) with delivery webhooks
- Secrets in a vault, TLS, rate limits per caller
