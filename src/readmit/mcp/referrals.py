"""MCP server: referrals to the care team.

python -m readmit.mcp.referrals             # http://localhost:8104/mcp
"""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from readmit.mcp.common import WRITES, call, serve
from readmit.outreach import store

mcp = FastMCP(
    name="referrals",
    instructions="Refer the patient to a human service when a barrier needs one: a pharmacist for medication "
    "questions or confusion, transport for no ride, social work for housing or support, an "
    "interpreter for language needs.",
)


@mcp.tool(annotations=WRITES)
def create_referral(
    case_id: Annotated[str, Field(description="Outreach case id, e.g. C000856")],
    referral_type: Annotated[
        Literal["pharmacist", "transport", "social_work", "interpreter"], Field(description="Which service")
    ],
    reason: Annotated[str, Field(min_length=3, description="Why, in one sentence, for the receiving team")],
    idempotency_key: Annotated[
        str, Field(description="Unique per referral; a retry with the same key is not duplicated")
    ],
) -> dict:
    """Request a referral. The receiving team follows up with the patient directly."""
    return call(store.create_referral, case_id, referral_type, reason, idempotency_key)


if __name__ == "__main__":
    serve(mcp, "referrals")
