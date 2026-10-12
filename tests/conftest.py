"""Test-wide settings: tests never send traces to LangSmith, even if .env turns tracing on."""
import os

os.environ["LANGSMITH_TRACING"] = "false"
