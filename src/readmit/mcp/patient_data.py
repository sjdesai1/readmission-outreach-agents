"""MCP server: patient data, read only, minimum necessary.

    python -m readmit.mcp.patient_data          # http://localhost:8101/mcp
"""
import json
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from readmit.mcp.common import READ_ONLY, call, serve
from readmit.outreach import store

mcp = FastMCP(
    name="patient_data",
    instructions="Read-only patient context for outreach. Returns only what's needed to plan and personalize "
                 "outreach: first name, language, allowed channels, the recent stay, risk drivers and barriers. "
                 "Contact details (phone, email) are never returned; the messaging server handles delivery.",
)

CaseId = Annotated[str, Field(description="Outreach case id, e.g. C000856")]


@mcp.tool(annotations=READ_ONLY)
def get_patient_summary(case_id: CaseId) -> dict:
    """Patient context for one outreach case: first name, age, language, allowed contact channels,
    preferred channel and time window, whether telehealth is possible, the recent hospital stay,
    risk score with top risk drivers, and social barriers found in clinical notes."""
    return call(store.patient_summary, case_id)


@mcp.resource("patient://{case_id}/summary", mime_type="application/json")
def patient_summary_resource(case_id: str) -> str:
    """The same summary as a readable resource, for clients that load context instead of calling tools."""
    return json.dumps(call(store.patient_summary, case_id), default=str)


if __name__ == "__main__":
    serve(mcp, "patient_data")
