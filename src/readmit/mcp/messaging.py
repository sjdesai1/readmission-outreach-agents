"""MCP server: patient messaging.

python -m readmit.mcp.messaging             # http://localhost:8103/mcp
"""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from readmit.mcp.common import READ_ONLY, WRITES, call, serve
from readmit.outreach import store

mcp = FastMCP(
    name="messaging",
    instructions="Send messages to patients and read conversation threads. Only channels the patient opted into "
    "are allowed; the server refuses others and tells you which are allowed. You never need the "
    "patient's phone number or email: pass the case_id and the server delivers.",
)

CaseId = Annotated[str, Field(description="Outreach case id, e.g. C000856")]


@mcp.tool(annotations=WRITES)
def send_message(
    case_id: CaseId,
    channel: Annotated[Literal["portal", "sms", "email", "phone"], Field(description="Must be an allowed channel")],
    body: Annotated[str, Field(min_length=1, description="Message text (for phone: the call script)")],
    idempotency_key: Annotated[str, Field(description="Unique per message; a retry with the same key is not resent")],
    subject: Annotated[str | None, Field(description="Email or portal subject line")] = None,
    priority: Annotated[
        Literal["normal", "safety"],
        Field(description="safety only for emergency instructions and opt-out confirmations: sent even in quiet hours"),
    ] = "normal",
) -> dict:
    """Send one message on one channel. Portal is delivered to the inbox, SMS and email are sent,
    phone calls are queued for the dialer. Refused for channels without consent and for closed cases.
    Between 8 PM and 8 AM normal messages are held (status 'scheduled') and delivered at 8 AM."""
    return call(store.send_message, case_id, channel, body, idempotency_key, subject, priority)


@mcp.tool(annotations=READ_ONLY)
def get_thread(
    case_id: CaseId, limit: Annotated[int, Field(ge=1, le=50, description="Most recent N messages")] = 20
) -> list[dict]:
    """The conversation with the patient, oldest first: every message in and out, on every channel."""
    return call(store.get_thread, case_id, limit)


if __name__ == "__main__":
    serve(mcp, "messaging")
