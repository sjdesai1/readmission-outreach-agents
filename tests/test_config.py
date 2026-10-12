"""Unit tests that need no running services (these run in CI)."""
import importlib


def test_db_url_uses_psycopg3_and_env(monkeypatch):
    monkeypatch.setenv("APP_DB_HOST", "postgres")
    monkeypatch.setenv("APP_DB_NAME", "app")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    import readmit.config as cfg
    importlib.reload(cfg)
    url = cfg.Settings().db_url
    assert url.startswith("postgresql+psycopg://")
    assert "@postgres:" in url and url.endswith("/app")


def test_env_example_has_every_setting():
    """Every variable the code reads must be documented in .env.example."""
    import readmit.config as cfg
    example = (cfg.REPO_ROOT / ".env.example").read_text()
    for var in ["GEMINI_API_KEY", "LANGSMITH_API_KEY", "POSTGRES_PASSWORD", "LLM_MODEL", "LLM_FALLBACK_MODEL",
                "LLM_JUDGE_MODEL", "LLM_MIN_INTERVAL_S", "APP_DB_HOST", "MLFLOW_TRACKING_URI", "SMTP_HOST",
                "MAILPIT_API_URL", "AIRFLOW_API_URL"]:
        assert f"{var}=" in example, f"{var} missing from .env.example"
