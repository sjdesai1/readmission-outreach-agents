"""Deterministic guardrails: plain rules, no LLM. They run before and after the agents.

Input:  STOP keywords close the case, emergencies go straight to a nurse, injection attempts are flagged.
Output: no invented times, no contact details, no booking claims without a booking, fits the channel.
"""
import re

from readmit.agents.state import parse_dt

# Carrier-standard opt-out keywords (the whole message, ignoring case and punctuation)
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}

EMERGENCY = [r"chest pain", r"can'?t breathe", r"cannot breathe", r"trouble breathing", r"short(ness)? of breath",
             r"passed out", r"faint(ed|ing)", r"slurred speech", r"face (is )?droop", r"stroke",
             r"suicid", r"kill myself", r"end my life", r"overdos", r"bleeding (a lot|heavily|won'?t stop)",
             r"dolor de pecho", r"no puedo respirar"]

INJECTION = [r"ignore (all |any )?(previous|prior|above|your) (instructions|rules)", r"system prompt",
             r"you are now", r"developer mode", r"disregard (the|your) (rules|instructions)",
             r"reveal (your|the) (prompt|instructions)", r"act as (an? )?(admin|doctor|system)"]

CHANNEL_MAX_CHARS = {"sms": 320, "portal": 1200, "email": 1200, "phone": 900}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{8,}\d")
PLACEHOLDER_RE = re.compile(r"\[[A-Za-z _]+\]|\{[A-Za-z_]+\}")
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)
CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
          "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
DATE_EN_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.IGNORECASE)
DATE_ES_RE = re.compile(r"\b(\d{1,2}) de (" + "|".join(k for k in MONTHS if len(k) > 3) + r")\b", re.IGNORECASE)
BOOKED_CLAIM_RE = re.compile(
    r"\b(you'?re booked|is booked|have booked|booked you|confirmed|is scheduled|scheduled you"
    r"|queda (agendad|confirmad|reservad)|est[aá] (confirmad|agendad))",
    re.IGNORECASE,
)


def screen_inbound(text: str | None) -> dict:
    """Return {'action': 'continue' | 'opt_out' | 'emergency', 'flags': [...]}."""
    t = (text or "").strip().lower()
    if re.sub(r"[^a-z]", "", t) in STOP_WORDS:
        return {"action": "opt_out", "flags": ["stop_keyword"]}
    for pat in EMERGENCY:
        if re.search(pat, t):
            return {"action": "emergency", "flags": [f"emergency:{pat}"]}
    flags = [f"injection:{pat}" for pat in INJECTION if re.search(pat, t)]
    return {"action": "continue", "flags": flags}


def _allowed_times(slots: list[dict]) -> tuple[set, set]:
    times, dates = set(), set()
    for s in slots:
        dt = parse_dt(s["start_at"])
        times.add((dt.hour % 12, dt.minute))
        dates.add((dt.month, dt.day))
    return times, dates


def check_draft(body: str, channel: str | None, slots: list[dict], appointment: dict | None) -> list[str]:
    """Problems with a draft, in words the writer can act on. Empty list = passes."""
    problems = []
    body = body or ""
    if not body.strip():
        return ["message is empty"]
    limit = CHANNEL_MAX_CHARS.get(channel or "", 1200)
    if len(body) > limit:
        problems.append(f"too long for {channel}: {len(body)} characters, limit {limit}")
    if EMAIL_RE.search(body) or PHONE_RE.search(body):
        problems.append("contains an email address or phone number; never include contact details")
    if PLACEHOLDER_RE.search(body):
        problems.append("contains an unfilled placeholder like [Name]")
    known = slots + ([appointment] if appointment else [])
    times, dates = _allowed_times(known)
    for m in TIME_RE.finditer(body):
        h, mins = int(m.group(1)) % 12, int(m.group(2) or 0)
        if (h, mins) not in times:
            problems.append(f"mentions a time ({m.group(0).strip()}) that is not one of the offered slots")
    for m in CLOCK_RE.finditer(body):
        h, mins = int(m.group(1)) % 12, int(m.group(2))
        if (h, mins) not in times and not TIME_RE.search(body[m.start():m.end() + 6]):
            problems.append(f"mentions a time ({m.group(0)}) that is not one of the offered slots")
    for m in DATE_EN_RE.finditer(body):
        if (MONTHS[m.group(1).lower()[:3]], int(m.group(2))) not in dates:
            problems.append(f"mentions a date ({m.group(0)}) that is not one of the offered slots")
    for m in DATE_ES_RE.finditer(body):
        if (MONTHS[m.group(2).lower()], int(m.group(1))) not in dates:
            problems.append(f"mentions a date ({m.group(0)}) that is not one of the offered slots")
    if not appointment and BOOKED_CLAIM_RE.search(body):
        problems.append("says the visit is booked or confirmed, but nothing was booked")
    return list(dict.fromkeys(problems))     # dedupe, keep order
