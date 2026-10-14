"""Keep personal details out of LLM prompts and traces.

The LLM never sees the patient's name: it writes {first_name} and code fills it in at send time.
Emails and phone numbers in free text are masked before any prompt, and again in LangSmith traces.
"""

import copy
import logging
import re

log = logging.getLogger(__name__)

NAME_TOKEN = "{first_name}"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{8,}\d")


def mask_contacts(text: str | None) -> str | None:
    if not text:
        return text
    return PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", text))


def pseudonymize(text: str | None, name: str | None) -> str | None:
    """Replace the patient's first name with the {first_name} token, and mask contact details."""
    if not text:
        return text
    if name:
        text = re.sub(rf"\b{re.escape(name)}\b", NAME_TOKEN, text, flags=re.IGNORECASE)
    return mask_contacts(text)


def render(text: str, name: str | None) -> str:
    """Fill the {first_name} token just before sending."""
    return text.replace(NAME_TOKEN, name or "there")


def pseudonymize_summary(summary: dict) -> tuple[dict, str | None]:
    """Return (summary with the name replaced by the token, the real name)."""
    s = copy.deepcopy(summary)
    name = s.get("patient", {}).get("first_name")
    if name:
        s["patient"]["first_name"] = NAME_TOKEN
    return s, name


def redact(obj):
    """Mask contact details anywhere in a nested structure (used as the LangSmith input/output hook)."""
    if isinstance(obj, str):
        return mask_contacts(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    return obj


def configure_tracing() -> None:
    """Install `redact` on the shared LangSmith client, so every trace (LLM calls and graph nodes) is masked.
    Best effort: if the client was already created elsewhere, the token-based pseudonymization still holds."""
    try:
        from langsmith import run_trees

        run_trees.get_cached_client(hide_inputs=redact, hide_outputs=redact)
    except Exception as e:  # noqa: BLE001 - tracing setup must never stop a turn
        log.warning("trace redaction not installed: %s", e)
