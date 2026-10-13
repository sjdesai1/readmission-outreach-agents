"""MCP servers: the only doors agents have into the outreach system.

    patient_data  :8101  read-only patient summary (minimum necessary)
    scheduling    :8102  clinic time, open slots, book / reschedule / cancel
    messaging     :8103  send on an opted-in channel, read the thread
    referrals     :8104  pharmacist, transport, social work, interpreter
    cases         :8105  case status, notes, history

Each server is a thin layer over readmit.outreach.store, where the rules live. Agents get least
privilege by connecting only to the servers their job needs.
"""
