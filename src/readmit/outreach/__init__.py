"""The outreach system of record: clinic calendar, cases, messages, appointments, referrals.

Business rules live in store.py. The MCP servers (readmit/mcp/) are thin interfaces over it,
so every rule is enforced no matter which agent, script or test calls it.
"""
