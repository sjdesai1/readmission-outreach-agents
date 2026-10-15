"""The agents' only way into the outreach system: MCP tool calls, scoped per role (least privilege).

Gateway.http()        the five MCP servers over HTTP (the running stack)
Gateway.in_memory()   the same servers in-process (tests; same protocol, no network)
"""

import json

from fastmcp import Client
from fastmcp.exceptions import ToolError

from readmit.config import settings
from readmit.mcp.common import PORTS

# Which tools each role may call. Anything not listed is refused before it reaches a server.
ACCESS: dict[str, set[str]] = {
    "load_context": {
        "patient_data.get_patient_summary",
        "cases.get_case",
        "messaging.get_thread",
        "scheduling.get_current_time",
    },
    "care_planner": {"patient_data.get_patient_summary", "referrals.create_referral"},
    "reply_interpreter": set(),
    "scheduler": {
        "scheduling.get_current_time",
        "scheduling.find_slots",
        "scheduling.book_appointment",
        "scheduling.reschedule_appointment",
    },
    "message_writer": set(),
    "safety_reviewer": set(),
    "send": {
        "patient_data.get_patient_summary",
        "messaging.send_message",
        "cases.update_case_status",
        "cases.add_case_note",
    },
    "escalate": {
        "messaging.send_message",
        "cases.create_nurse_task",
        "cases.update_case_status",
        "cases.add_case_note",
    },
    "remember": {"patient_data.remember_preference"},
    "hold_for_nurse": {"cases.add_case_note"},
    "close_unreachable": {"cases.add_case_note", "cases.update_case_status"},
    "opt_out": {"messaging.send_message", "cases.update_case_status"},
    "proxy_reply": {"messaging.send_message", "cases.add_case_note", "cases.update_case_status"},
    "apply_channel_change": {"cases.set_preferred_channel"},
    "identity_check": {"patient_data.verify_identity", "messaging.send_message", "cases.add_case_note"},
}


class ToolFailed(Exception):
    """The server refused (a business rule or invalid arguments). The message says why and what to do instead."""

    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool, self.message = tool, message


class Gateway:
    def __init__(self, targets: dict, role: str | None = None):
        self.targets = targets  # server name -> URL or in-process FastMCP server
        self.role = role  # None: unscoped, may call nothing

    @classmethod
    def http(cls, host: str | None = None) -> "Gateway":
        host = host or settings.mcp_host
        return cls({name: f"http://{host}:{port}/mcp" for name, port in PORTS.items()})

    @classmethod
    def in_memory(cls) -> "Gateway":
        from readmit.mcp import cases, messaging, patient_data, referrals, scheduling

        return cls({m.mcp.name: m.mcp for m in (patient_data, scheduling, messaging, referrals, cases)})

    def scoped(self, role: str) -> "Gateway":
        return Gateway(self.targets, role)

    async def list_tools(self, server: str, hide: tuple[str, ...] = ("case_id", "idempotency_key")) -> list[dict]:
        """Tool discovery: the server's tool names, descriptions and input schemas, filtered to this role.
        Parameters in `hide` are removed from the schema: code supplies them, the model never chooses them."""
        allowed = ACCESS.get(self.role or "", set())
        async with Client(self.targets[server]) as client:
            tools = await client.list_tools()
        out = []
        for t in tools:
            if f"{server}.{t.name}" not in allowed:
                continue
            schema = dict(t.inputSchema or {})
            props = {k: v for k, v in (schema.get("properties") or {}).items() if k not in hide}
            required = [r for r in schema.get("required") or [] if r not in hide]
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {"type": "object", "properties": props, "required": required},
                }
            )
        return out

    async def call(self, server: str, tool: str, **args):
        name = f"{server}.{tool}"
        if name not in ACCESS.get(self.role or "", set()):
            raise PermissionError(f"role {self.role!r} may not call {name}")
        args = {k: v for k, v in args.items() if v is not None}
        try:
            async with Client(self.targets[server]) as client:
                result = await client.call_tool(tool, args)
        except ToolError as e:
            raise ToolFailed(name, str(e)) from e
        if not result.content:
            return None
        text = result.content[0].text
        try:
            return json.loads(text)
        except ValueError:
            return text  # plain-string results, e.g. get_current_time
