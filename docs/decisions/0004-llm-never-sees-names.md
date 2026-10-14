# 0004 The LLM never sees patient names

Status: Accepted

## Context
- Prompts and traces leave the system: model provider, LangSmith, logs
- Minimum necessary: the model needs context to write a good message, not identity

## Decision
- load_context replaces the first name with the {first_name} token in the summary and thread
- The writer outputs the token; the send node looks up the real name at the last moment and fills it in
- Emails and phone numbers are masked in prompts, and again in traces through a LangSmith redaction hook
- The diagnosis never appears in SMS, email or phone messages; allowed only in the logged-in portal

## Consequences
- Names never reach a prompt, the graph state or a trace (enforced by a test)
- Slightly less natural phrasing options for the writer; no measurable quality loss so far
- Contact details are resolved only by the messaging server at delivery time
