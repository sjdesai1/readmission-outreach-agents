"""Settings in one place. Every value comes from environment variables (.env on the laptop,
docker-compose.yml overrides inside containers), so the same code runs in both places."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env", override=False)  # real environment variables win over .env


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # database for the app's own data
    db_host: str = field(default_factory=lambda: _env("APP_DB_HOST", "localhost"))
    db_port: int = field(default_factory=lambda: int(_env("APP_DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: _env("APP_DB_NAME", "app"))
    db_user: str = field(default_factory=lambda: _env("APP_DB_USER", "postgres"))
    db_password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", "postgres"))
    # services
    mlflow_uri: str = field(default_factory=lambda: _env("MLFLOW_TRACKING_URI", "http://localhost:5000"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", "localhost"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "1025")))
    mailpit_api: str = field(default_factory=lambda: _env("MAILPIT_API_URL", "http://localhost:8025"))
    airflow_api: str = field(default_factory=lambda: _env("AIRFLOW_API_URL", "http://localhost:8080"))
    grafana_url: str = field(default_factory=lambda: _env("GRAFANA_URL", "http://localhost:3000"))
    # MCP servers (agents reach the outreach system only through these)
    mcp_host: str = field(default_factory=lambda: _env("MCP_HOST", "localhost"))
    # LLM
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "gemini/gemini-3.5-flash-lite"))
    llm_fallback_model: str = field(default_factory=lambda: _env("LLM_FALLBACK_MODEL", "gemini/gemini-3.1-flash-lite"))
    llm_judge_model: str = field(default_factory=lambda: _env("LLM_JUDGE_MODEL", "gemini/gemini-3.5-flash"))
    llm_min_interval_s: float = field(default_factory=lambda: float(_env("LLM_MIN_INTERVAL_S", "4")))
    # spend cap: once real (uncached) calls today cost this much, agents switch to the rule-based versions
    llm_daily_budget_usd: float = field(default_factory=lambda: float(_env("LLM_DAILY_BUDGET_USD", "1.00")))
    # tracing
    langsmith_api_key: str = field(default_factory=lambda: _env("LANGSMITH_API_KEY"))
    langsmith_project: str = field(default_factory=lambda: _env("LANGSMITH_PROJECT", "readmission-outreach"))

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL using the psycopg (v3) driver."""
        return f"postgresql+psycopg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


settings = Settings()
