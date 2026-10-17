"""Outreach cadence: when to try again, on which channel, and when to stop. Pure rules, no I/O.

    attempt 1  day 0     the planner's channel
    attempt 2  +48h      same channel, a short follow-up
    attempt 3  +48h      a phone call, if the patient allows calls
    no reply   +48h      case closed as unreachable (a nurse can still reopen it)

The 48h spacing comes from the send node (next_action_at). Outreach turns only run in daytime hours;
quiet hours still hold anything sent outside 8 AM to 8 PM.
"""

from datetime import datetime

MAX_ATTEMPTS = 3
PHONE_ATTEMPT = 3  # last try: a person's voice reaches people who ignore texts and emails
OUTREACH_HOURS = (9, 19)  # the sequencer starts outreach turns only between 9 AM and 7 PM


def unanswered(thread: list[dict]) -> int:
    """AI outreach messages since the patient last wrote (the whole thread, oldest first). A nurse's note is not
    an outreach attempt, so it doesn't count toward giving up."""
    n = 0
    for m in thread:
        if m.get("author") == "nurse":
            continue
        n = 0 if m["direction"] == "inbound" else n + 1
    return n


def next_attempt(sent_unanswered: int, allowed_channels: list[str]) -> dict:
    """What the next outreach turn should be: an attempt number and channel, or close."""
    if sent_unanswered >= MAX_ATTEMPTS:
        return {"close": True, "reason": f"no reply after {MAX_ATTEMPTS} attempts"}
    attempt = sent_unanswered + 1
    channel = "phone" if attempt == PHONE_ATTEMPT and "phone" in allowed_channels else None
    return {"close": False, "attempt": attempt, "channel": channel}


def in_outreach_hours(t: datetime) -> bool:
    return OUTREACH_HOURS[0] <= t.hour < OUTREACH_HOURS[1]
