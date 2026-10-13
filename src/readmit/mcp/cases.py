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
Status = Literal["new", "suppressed", "planned", "contacted", "awaiting_reply", "booking", "booked", "reminded",
                 "completed", "no_show", "escalated", "opted_out", "readmitted", "closed"]


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


@mcp.tool(annotations={"readOnlyHint": False})
def add_case_note(case_id: CaseId, note: Annotated[str, Field(min_length=3)]) -> dict:
    """Add a free-text note to the case history, e.g. what the patient said or why a decision was made."""
    return call(store.add_note, case_id, note)


if __name__ == "__main__":
    serve(mcp, "cases")
