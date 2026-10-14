"""Deterministic guardrails: plain rules, no LLM. They run before and after the agents.

Input:  STOP keywords close the case, emergencies go straight to a nurse, someone other than the patient gets
        a privacy-safe reply, injection attempts are flagged.
Output: no invented times, no contact details, no diagnosis, no booking claims without a booking, fits the channel,
        respectful tone (no shouting, no pressure).
"""
import re
from datetime import datetime, timedelta

from readmit.agents.state import parse_dt

# Carrier-standard opt-out keywords (the whole message, ignoring case and punctuation)
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}

EMERGENCY = [r"chest pain", r"can'?t breathe", r"cannot breathe", r"trouble breathing", r"short(ness)? of breath",
             r"passed out", r"faint(ed|ing)", r"slurred speech", r"face (is )?droop", r"stroke",
             r"suicid", r"kill myself", r"end my life", r"overdos", r"bleeding (a lot|heavily|won'?t stop)",
             r"dolor de pecho", r"no puedo respirar"]

# Someone other than the patient replying (family member, friend). They get no health details.
RELATIONS = (r"daughter|son|wife|husband|spouse|partner|mother|father|mom|dad|sister|brother|caregiver|niece|"
             r"nephew|friend|neighbou?r|grandson|granddaughter")
PROXY = [rf"\b(this is|i'?m|i am) (his|her|their|the patient'?s|[a-z]+'s) ({RELATIONS})\b",
         rf"\b(his|her) ({RELATIONS}) here\b", r"\bon (his|her|their) behalf\b",
         r"\b(he|she) (is|'s) (asleep|sleeping|not available|not here|busy)\b",
         r"\bsoy su (hija|hijo|esposa|esposo|madre|padre|hermana|hermano|nieta|nieto)\b"]

INJECTION = [r"ignore (all |any )?(previous|prior|above|your) (instructions|rules)", r"system prompt",
             r"you are now", r"developer mode", r"disregard (the|your) (rules|instructions)",
             r"reveal (your|the) (prompt|instructions)", r"act as (an? )?(admin|doctor|system)"]

CHANNEL_MAX_CHARS = {"sms": 320, "portal": 1200, "email": 1200, "phone": 900}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{8,}\d")
PLACEHOLDER_RE = re.compile(r"\[[A-Za-z _]+\]|\{[A-Za-z_]+\}")
ALLOWED_TOKENS = {"{first_name}"}            # filled in at send time
SHOUTING_RE = re.compile(r"\b[A-Z]{4,}\b")
SHOUT_OK = {"STOP", "START", "HELP"}
PRESSURE_RE = re.compile(r"\b(urgent|urgently|immediately|you must|failure to|final notice|last chance|"
                         r"or else|required to respond|act now)\b", re.IGNORECASE)
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
    """Return {'action': 'continue' | 'opt_out' | 'emergency' | 'third_party', 'flags': [...]}.
    Order matters: an emergency reported by a family member is still an emergency."""
    t = (text or "").strip().lower()
    if re.sub(r"[^a-z]", "", t) in STOP_WORDS:
        return {"action": "opt_out", "flags": ["stop_keyword"]}
    for pat in EMERGENCY:
        if re.search(pat, t):
            return {"action": "emergency", "flags": [f"emergency:{pat}"]}
    flags = [f"injection:{pat}" for pat in INJECTION if re.search(pat, t)]
    for pat in PROXY:
        if re.search(pat, t):
            return {"action": "third_party", "flags": [*flags, f"third_party:{pat}"]}
    return {"action": "continue", "flags": flags}


def _allowed_times(slots: list[dict]) -> tuple[set, set]:
    times, dates = set(), set()
    for s in slots:
        dt = parse_dt(s["start_at"])
        times.add((dt.hour % 12, dt.minute))
        dates.add((dt.month, dt.day))
    return times, dates


def check_draft(body: str, channel: str | None, slots: list[dict], appointment: dict | None,
                private_terms: list[str] | None = None) -> list[str]:
    """Problems with a draft, in words the writer can act on. Empty list = passes.
    private_terms: words that must never appear in a message (e.g. the diagnosis): messages can be read by
    anyone holding the phone or inbox."""
    problems = []
    body = body or ""
    if not body.strip():
        return ["message is empty"]
    limit = CHANNEL_MAX_CHARS.get(channel or "", 1200)
    if len(body) > limit:
        problems.append(f"too long for {channel}: {len(body)} characters, limit {limit}")
    if EMAIL_RE.search(body) or PHONE_RE.search(body):
        problems.append("contains an email address or phone number; never include contact details")
    for term in private_terms or []:
        if term and re.search(rf"\b{re.escape(term)}\b", body, re.IGNORECASE):
            problems.append(f"mentions a private health detail ({term}); keep messages free of diagnoses")
    if any(m.group(0) not in ALLOWED_TOKENS for m in PLACEHOLDER_RE.finditer(body)):
        problems.append("contains an unfilled placeholder like [Name]; use only {first_name}")
    if any(w not in SHOUT_OK for w in SHOUTING_RE.findall(body)):
        problems.append("uses ALL CAPS words; write in normal case")
    if body.count("!") > 1:
        problems.append("more than one exclamation mark; keep the tone calm")
    if m := PRESSURE_RE.search(body):
        problems.append(f"pressuring language ({m.group(0)}); be warm and never pushy")
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


# ------------------------------------------------------------------ booking guard
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sáb": 5, "sab": 5, "dom": 6}


def requested_weekdays(details: dict) -> set[int]:
    days = set()
    for d in details.get("preferred_days") or []:
        key = d.strip().lower()[:3]
        if key in WEEKDAYS:
            days.add(WEEKDAYS[key])
    return days


def _hhmm(t: str | None) -> tuple[int, int] | None:
    if not t:
        return None
    h, m = t.split(":")
    return int(h), int(m)


def slot_fits(start_at, details: dict) -> list[str]:
    """Why a slot contradicts what the patient asked for. Empty = it fits. Checked before ANY booking."""
    dt = start_at if isinstance(start_at, datetime) else parse_dt(start_at)
    problems = []
    days = requested_weekdays(details)
    if days and dt.weekday() not in days:
        problems.append(f"patient asked for {details['preferred_days']}, slot is a {dt:%A}")
    if (e := _hhmm(details.get("earliest_time"))) and (dt.hour, dt.minute) < e:
        problems.append(f"patient can't do before {details['earliest_time']}, slot is {dt:%H:%M}")
    if (lt := _hhmm(details.get("latest_time"))) and (dt.hour, dt.minute) >= lt:
        problems.append(f"patient can't do from {details['latest_time']}, slot is {dt:%H:%M}")
    pod = details.get("part_of_day")
    if pod == "morning" and dt.hour >= 12 or pod == "afternoon" and dt.hour < 12:
        problems.append(f"patient asked for {pod}, slot is {dt:%H:%M}")
    return problems


def search_window(details: dict, now: str) -> tuple[str | None, str | None]:
    """Earliest/latest datetimes for find_slots from the patient's request (next matching weekday)."""
    days = requested_weekdays(details)
    if not days:
        return None, None
    today = parse_dt(now).replace(hour=0, minute=0, second=0, microsecond=0)
    day = next(today + timedelta(days=i) for i in range(7) if (today + timedelta(days=i)).weekday() in days)
    e = _hhmm(details.get("earliest_time")) or (0, 0)
    return (day.replace(hour=e[0], minute=e[1]).isoformat(timespec="minutes"),
            (day + timedelta(days=1)).isoformat(timespec="minutes"))
