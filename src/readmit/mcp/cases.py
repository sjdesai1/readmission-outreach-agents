"""MCP server: outreach case records.

python -m readmit.mcp.cases                 # http://localhost:8105/mcp
"""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from readmit.mcp.common import READ_ONLY, call, serve
from readmit.outreach import store

mcp = FastMCP(
    name="cases",
    instructions="Read and update outreach cases. Every change is written to the case's audit log. "
    "Closed cases (completed, opted_out, readmitted, closed, suppressed) cannot be reopened.",
)

CaseId = Annotated[str, Field(description="Outreach case id, e.g. C000856")]
Status = Literal[
    "new",
    "suppressed",
    "planned",
    "contacted",
    "awaiting_reply",
    "booking",
    "booked",
    "reminded",
    "completed",
    "no_show",
    "escalated",
    "opted_out",
    "readmitted",
    "closed",
]


@mcp.tool(annotations=READ_ONLY)
def get_case(case_id: CaseId) -> dict:
    """The case: status, tier, risk, attempts so far, next action time, and its most recent events."""
    return call(store.get_case, case_id)


@mcp.tool(annotations={"readOnlyHint": False})
def update_case_status(
    case_id: CaseId,
    status: Annotated[Status, Field(description="New status")],
    reason: Annotated[str, Field(min_length=3, description="Why, in one sentence (goes in the audit log)")],
    next_action_at: Annotated[str | None, Field(description="When to act next, ISO datetime")] = None,
) -> dict:
    """Move the case to a new status, optionally scheduling the next action."""
    return call(store.update_case, case_id, status, reason, next_action_at)


@mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
def set_preferred_channel(
    case_id: CaseId,
    channel: Annotated[Literal["portal", "sms", "email", "phone"], Field(description="Channel the patient asked for")],
    reason: Annotated[str, Field(min_length=3, description="What the patient said, briefly (goes in the audit log)")],
) -> dict:
    """Record the patient's request to be contacted another way. Refused if that contact detail isn't on file.
    Returns the updated allowed channels and preference."""
    return call(store.set_preferred_channel, case_id, channel, reason)


@mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
def create_nurse_task(
    case_id: CaseId,
    kind: Annotated[
        Literal["emergency", "symptom", "clinical_question", "callback", "review"], Field(description="Why a nurse")
    ],
    priority: Annotated[Literal["urgent", "routine"], Field(description="urgent: within the hour")],
    reason: Annotated[str, Field(min_length=3, description="One sentence for the nurse")],
    idempotency_key: Annotated[str, Field(description="Unique per handoff; a retry is not duplicated")],
    summary: Annotated[str | None, Field(description="Short context for the nurse")] = None,
) -> dict:
    """Hand the case to a nurse. Returns the task with an honest due time (urgent within the hour, routine by
    5 PM on the next business day with capacity). The AI pauses on this case until a nurse hands it back."""
    return call(store.create_nurse_task, case_id, kind, priority, reason, idempotency_key, summary)


@mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
def record_barrier(
    case_id: CaseId,
    detail: Annotated[str, Field(min_length=2, description="The barrier in the patient's words, briefly")],
    source: Annotated[str, Field(description="Where it came from, e.g. 'patient reply'")] = "patient reply",
) -> dict:
    """Record a barrier the patient told us about (no ride, cost, work). Named like the model's barriers, so the
    planner can act on it (a transport referral, a video visit)."""
    return call(store.record_barrier, case_id, detail, source)


@mcp.tool(annotations={"readOnlyHint": False})
def add_case_note(case_id: CaseId, note: Annotated[str, Field(min_length=3)]) -> dict:
    """Add a free-text note to the case history, e.g. what the patient said or why a decision was made."""
    return call(store.add_note, case_id, note)


if __name__ == "__main__":
    serve(mcp, "cases")
