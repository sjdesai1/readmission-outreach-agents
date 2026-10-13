"""Shared plumbing for the MCP servers."""
import argparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from readmit.outreach.store import StoreError

PORTS = {"patient_data": 8101, "scheduling": 8102, "messaging": 8103, "referrals": 8104, "cases": 8105}
READ_ONLY = {"readOnlyHint": True}          # MCP tool annotations: tell clients which tools change nothing
WRITES = {"readOnlyHint": False, "idempotentHint": True}


def call(fn, *args, **kwargs):
    """Run a store function; a broken rule becomes a ToolError the agent can read and act on."""
    try:
        return fn(*args, **kwargs)
    except StoreError as e:
        raise ToolError(str(e)) from e


def serve(mcp: FastMCP, name: str) -> None:
    """HTTP by default (how agents connect); --stdio for local debugging tools."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true")
    args = ap.parse_args()
    if args.stdio:
        mcp.run()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=PORTS[name])
