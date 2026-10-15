"""What one turn knows. Every node reads the state and returns only the keys it changes."""

import operator
from datetime import datetime
from typing import Annotated, Literal, TypedDict

Trigger = Literal["outreach_due", "inbound_reply"]
MAX_REVISIONS = 1  # a draft gets one rewrite; failing again goes to a nurse
DAY_KEYS = ("preferred_days",)
TIME_KEYS = ("part_of_day", "earliest_time", "latest_time")
MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DAYS_ES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


class TurnState(TypedDict, total=False):
    # input
    case_id: str
    trigger: Trigger
    inbound: str | None  # the patient's reply (inbound_reply only)
    inbound_channel: str | None
    # loaded by load_context
    now: str  # simulation time, ISO
    turn_key: str  # deterministic per turn: the base of every idempotency key
    summary: dict  # minimum-necessary patient summary
    case: dict
    thread: list[dict]  # recent messages, oldest first
    history: str  # one-line digest of older messages
    unclear_streak: int  # how many replies in a row we couldn't understand
    skip: str | None  # reason to do nothing this turn
    # decisions
    guard: dict  # input guard result: action + flags
    intent: dict  # reply interpreter: intent, details, accepts
    plan: dict  # care planner: goal, channel, talking points, referrals
    schedule: dict  # scheduler: offered slots, appointment, error
    draft: dict  # message writer: subject, body
    problems: list[str]  # output check failures (fed back to the writer)
    review: dict  # safety reviewer: verdict, reasons
    revisions: int
    identity: dict  # phone identity check: asked / verified
    reply_on: str | None  # channel the patient just switched to (overrides the inbound channel)
    channel_change: dict  # result of a requested channel change
    remembered: list[str]  # standing preferences saved this turn ("I work mornings")
    escalate_reason: str | None
    urgent: bool
    nurse_task: dict  # the handoff created by escalate (kind, priority, honest due time)
    outcome: dict  # what the turn finally did
    path: Annotated[list[str], operator.add]  # nodes visited, appended by each node (for tests and traces)


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", ""))


def when(start_at) -> str:
    """One display format for every slot time, e.g. 'Thu Sep 10, 9:00 AM'. Output checks rely on it."""
    dt = start_at if isinstance(start_at, datetime) else parse_dt(start_at)
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def when_es(start_at) -> str:
    """Spanish display format, e.g. 'jue 10 sep, 9:00 AM' (used only in fixed Spanish templates)."""
    dt = start_at if isinstance(start_at, datetime) else parse_dt(start_at)
    return f"{DAYS_ES[dt.weekday()]} {dt.day} {MONTHS_ES[dt.month - 1]}, {dt.hour % 12 or 12}:{dt:%M} " + (
        "AM" if dt.hour < 12 else "PM"
    )


def request_details(state: TurnState) -> dict:
    """What the patient wants for scheduling: this reply's details on top of their standing preferences.
    Days and time of day combine ("Monday?" + remembered "afternoons" = Monday afternoon). Within one
    dimension the reply wins as a whole: "after 2" replaces a remembered "mornings", never mixes with it."""
    memory = dict((state.get("summary") or {}).get("memory") or {})
    details = dict(((state.get("intent") or {}).get("details")) or {})
    for group in (DAY_KEYS, TIME_KEYS):
        if any(details.get(k) for k in group):
            memory = {k: v for k, v in memory.items() if k not in group}
    return {**memory, **{k: v for k, v in details.items() if v not in (None, [], "")}}


def reply_channel(state: TurnState) -> str | None:
    """Answer on the channel the patient asked for, else the one they used, else the planner's choice."""
    if state.get("reply_on"):
        return state["reply_on"]
    if state.get("trigger") == "inbound_reply" and state.get("inbound_channel"):
        return state["inbound_channel"]
    return (state.get("plan") or {}).get("channel")
