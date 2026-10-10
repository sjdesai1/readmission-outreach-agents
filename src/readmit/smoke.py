"""Milestone 0 smoke test: proves every service in the stack is up and reachable.

Run from your laptop:          python -m readmit.smoke
Run inside Airflow (via DAG):  m0_smoke DAG -> same module, container hostnames

Each check is independent: one failure doesn't hide the others.
Exit code 1 if anything fails, so CI or a Makefile can stop on it.
"""
import smtplib
import sys
import time
import uuid
from email.message import EmailMessage

import requests

from readmit.config import settings

TIMEOUT = 15
results: list[tuple[str, str, str]] = []   # (check, status, detail)


def check(name):
    """Decorator: run a check, record PASS / FAIL / SKIP with a one-line detail."""
    def wrap(fn):
        def run():
            try:
                detail = fn()
                status = "SKIP" if isinstance(detail, str) and detail.startswith("SKIP") else "PASS"
                results.append((name, status, detail.removeprefix("SKIP: ") if detail else ""))
            except Exception as e:  # noqa: BLE001 - a smoke test reports every failure, whatever it is
                results.append((name, "FAIL", f"{type(e).__name__}: {str(e)[:160]}"))
        return run
    return wrap


@check("postgres (app db)")
def postgres():
    import sqlalchemy as sa
    eng = sa.create_engine(settings.db_url, connect_args={"connect_timeout": TIMEOUT})
    with eng.connect() as c:
        version = c.execute(sa.text("select version()")).scalar()
    return version.split(",")[0]


@check("fhir server (HAPI)")
def fhir():
    base = settings.fhir_base_url
    meta = requests.get(f"{base}/metadata", timeout=60)
    meta.raise_for_status()
    fhir_version = meta.json().get("fhirVersion")
    # prove write + read + delete works with a throwaway Patient
    pid = f"smoke-{uuid.uuid4().hex[:8]}"
    body = {"resourceType": "Patient", "id": pid, "name": [{"family": "Smoke", "given": ["Test"]}]}
    requests.put(f"{base}/Patient/{pid}", json=body, timeout=TIMEOUT).raise_for_status()
    got = requests.get(f"{base}/Patient/{pid}", timeout=TIMEOUT).json()
    requests.delete(f"{base}/Patient/{pid}", timeout=TIMEOUT)
    assert got["name"][0]["family"] == "Smoke"
    return f"FHIR {fhir_version}, write/read/delete ok"


@check("mlflow tracking + artifacts")
def mlflow_check():
    import mlflow
    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("m0-smoke")
    with mlflow.start_run(run_name="smoke") as run:
        mlflow.log_param("check", "m0")
        mlflow.log_metric("ok", 1.0)
        mlflow.log_text("smoke test artifact", "smoke.txt")   # proves artifact storage works
    return f"run {run.info.run_id[:8]} logged"


@check("mailpit (email)")
def mailpit():
    subject = f"smoke {uuid.uuid4().hex[:6]}"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "outreach@orchardvalley.example", "patient@example.com", subject
    msg.set_content("Milestone 0 smoke test.")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT) as s:
        s.send_message(msg)
    time.sleep(1)
    inbox = requests.get(f"{settings.mailpit_api}/api/v1/messages", timeout=TIMEOUT).json()
    assert any(m.get("Subject") == subject for m in inbox.get("messages", [])), "sent email not found in inbox"
    return "sent and found in inbox"


@check("airflow api")
def airflow():
    r = requests.get(f"{settings.airflow_api}/api/v2/monitor/health", timeout=TIMEOUT)
    r.raise_for_status()
    status = r.json().get("scheduler", {}).get("status")
    return f"scheduler {status}"


@check("gemini key + models")
def gemini_models():
    if not settings.gemini_api_key:
        return "SKIP: GEMINI_API_KEY not set"
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": settings.gemini_api_key, "pageSize": 200}, timeout=TIMEOUT)
    r.raise_for_status()
    names = sorted(m["name"].removeprefix("models/") for m in r.json().get("models", [])
                   if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in m["name"])
    return f"{len(names)} models, e.g. " + ", ".join(names[:8])


@check("gemini call via litellm + langsmith trace")
def gemini_call():
    if not settings.gemini_api_key:
        return "SKIP: GEMINI_API_KEY not set"
    import litellm
    from langsmith import Client, traceable

    client = Client() if settings.langsmith_api_key else None

    @traceable(name="m0_smoke_llm_call", client=client, tags=["m0", "smoke"])
    def ask(model):
        resp = litellm.completion(model=model, timeout=30,
                                  messages=[{"role": "user", "content": "Reply with exactly: OK"}])
        return resp.choices[0].message.content.strip()

    used, answer = settings.llm_model, None
    try:
        answer = ask(used)
    except Exception:  # noqa: BLE001 - primary failed for any reason: try the fallback
        used = settings.llm_fallback_model
        answer = ask(used)
    if client is not None and hasattr(client, "flush"):
        client.flush()   # send the trace before the process exits
    traced = "trace sent" if client else "LangSmith key not set, no trace"
    return f"{used} answered {answer!r}; {traced}"


@check("langsmith key")
def langsmith_key():
    if not settings.langsmith_api_key:
        return "SKIP: LANGSMITH_API_KEY not set"
    from langsmith import Client
    list(Client().list_projects(limit=1))
    return f"key valid, project '{settings.langsmith_project}'"


def main() -> int:
    for fn in (postgres, fhir, mlflow_check, mailpit, airflow, gemini_models, gemini_call, langsmith_key):
        fn()
    width = max(len(n) for n, _, _ in results)
    print()
    for name, status, detail in results:
        print(f"  {status:4}  {name:<{width}}  {detail}")
    failed = [n for n, s, _ in results if s == "FAIL"]
    print(f"\n  {len(results) - len(failed)}/{len(results)} passed or skipped" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
