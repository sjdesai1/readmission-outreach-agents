"""The MCP servers, called the way an agent calls them: through an MCP client (in memory, no network)."""
import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from readmit.mcp import cases, messaging, patient_data, referrals, scheduling

pytestmark = pytest.mark.usefixtures("pg")


def run(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            result = await client.call_tool(tool, args or {})
            return json.loads(result.content[0].text) if result.content else None
    return asyncio.run(go())


def tool_names(server):
    async def go():
        async with Client(server.mcp) as client:
            return sorted(t.name for t in await client.list_tools())
    return asyncio.run(go())


def test_each_server_exposes_only_its_own_tools():
    assert tool_names(patient_data) == ["get_patient_summary"]
    assert tool_names(scheduling) == ["book_appointment", "cancel_appointment", "find_slots", "get_current_time",
                                      "reschedule_appointment"]
    assert tool_names(messaging) == ["get_thread", "send_message"]
    assert tool_names(referrals) == ["create_referral"]
    assert tool_names(cases) == ["add_case_note", "get_case", "set_preferred_channel", "update_case_status"]


def test_summary_through_mcp():
    s = run(patient_data, "get_patient_summary", {"case_id": "C1"})
    assert s["patient"]["first_name"] == "Maria" and "maria@example.com" not in json.dumps(s)


def test_find_then_book_through_mcp():
    slots = run(scheduling, "find_slots", {"case_id": "C1", "limit": 2})
    appt = run(scheduling, "book_appointment", {"case_id": "C1", "slot_id": slots[0]["slot_id"],
                                                "idempotency_key": "mcp-1"})
    assert appt["status"] == "booked"


def test_rule_violation_reaches_the_agent_as_a_readable_error():
    with pytest.raises(ToolError, match="not opted into sms"):
        run(messaging, "send_message", {"case_id": "C1", "channel": "sms", "body": "hi", "idempotency_key": "m"})


def test_schema_rejects_invalid_values_before_any_rule_runs():
    with pytest.raises(ToolError):
        run(messaging, "send_message", {"case_id": "C1", "channel": "fax", "body": "hi", "idempotency_key": "m"})
