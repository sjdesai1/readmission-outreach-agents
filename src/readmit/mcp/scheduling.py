"""MCP server: clinic scheduling.

python -m readmit.mcp.scheduling            # http://localhost:8102/mcp
"""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from readmit.mcp.common import READ_ONLY, WRITES, call, serve
from readmit.outreach import store

mcp = FastMCP(
    name="scheduling",
    instructions="Find and book follow-up visits. Always call find_slots first and only offer slots it returned. "
    "Never invent dates or times. Use a new idempotency_key for each distinct booking; reuse the same "
    "key only when retrying the exact same request.",
)

CaseId = Annotated[str, Field(description="Outreach case id, e.g. C000856")]
Key = Annotated[str, Field(description="Unique key for this action; retrying with the same key never double-books")]


@mcp.tool(annotations=READ_ONLY)
def get_current_time() -> str:
    """The current date and time in the simulation (ISO format). Use it to interpret 'tomorrow', 'next week'."""
    return call(store.now).isoformat(timespec="minutes")


@mcp.tool(annotations=READ_ONLY)
def get_appointment(case_id: CaseId) -> dict | None:
    """The patient's next booked visit (time, visit type, doctor), or null if none."""
    return call(store.upcoming_appointment, case_id)


@mcp.tool(annotations=READ_ONLY)
def find_slots(
    case_id: CaseId,
    earliest: Annotated[str | None, Field(description="Earliest start, ISO datetime, e.g. 2026-09-15T00:00")] = None,
    latest: Annotated[
        str | None, Field(description="Latest start, ISO datetime. Default: 14 days after earliest")
    ] = None,
    mode: Annotated[Literal["in_person", "telehealth"] | None, Field(description="Visit type; omit for both")] = None,
    part_of_day: Annotated[
        Literal["morning", "afternoon"] | None, Field(description="Morning (<12:00) or afternoon")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=10, description="How many options to return")] = 3,
) -> list[dict]:
    """Open follow-up slots this patient can use, soonest first. Telehealth is only offered if the patient
    consented and has a video device. Slots start at least 2 hours from now."""
    return call(store.find_slots, case_id, earliest, latest, mode, part_of_day, limit)


@mcp.tool(annotations=WRITES)
def book_appointment(
    case_id: CaseId, slot_id: Annotated[str, Field(description="slot_id from find_slots")], idempotency_key: Key
) -> dict:
    """Book a slot for the patient. Fails if the slot was taken, is too soon, or the patient already has an
    upcoming appointment (use reschedule_appointment then). Returns the appointment, with a video link for telehealth."""
    return call(store.book_appointment, case_id, slot_id, idempotency_key)


@mcp.tool(annotations=WRITES)
def reschedule_appointment(
    appointment_id: str, new_slot_id: Annotated[str, Field(description="slot_id from find_slots")], idempotency_key: Key
) -> dict:
    """Move an existing appointment to a new open slot. The old slot is released."""
    return call(store.reschedule_appointment, appointment_id, new_slot_id, idempotency_key)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def cancel_appointment(appointment_id: str, reason: str) -> dict:
    """Cancel an appointment and release the slot."""
    return call(store.cancel_appointment, appointment_id, reason)


if __name__ == "__main__":
    serve(mcp, "scheduling")
