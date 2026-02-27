"""Centralised configuration for the Jira Comment Assistant.

Reads from environment variables (or .env file via python-dotenv).
All settings have safe defaults for local development / CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (if present)
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_FILE)


@dataclass(frozen=True)
class JiraConfig:
    base_url: str = os.getenv("JIRA_BASE_URL", "")
    username: str = os.getenv("JIRA_USERNAME", "")
    api_token: str = os.getenv("JIRA_API_TOKEN", "")


@dataclass(frozen=True)
class CopilotConfig:
    api_key: str = os.getenv("COPILOT_API_KEY", "")
    model: str = os.getenv("COPILOT_MODEL", "gpt-4")
    temperature: float = float(os.getenv("COPILOT_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("COPILOT_MAX_TOKENS", "1024"))


@dataclass(frozen=True)
class NotificationConfig:
    # Teams
    teams_webhook_url: str = os.getenv("TEAMS_WEBHOOK_URL", "")
    # Email / SMTP
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    email_from: str = os.getenv("EMAIL_FROM", "")
    email_to: str = os.getenv("EMAIL_TO", "")  # comma-separated


@dataclass(frozen=True)
class AppConfig:
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "8000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    max_comments: int = int(os.getenv("MAX_COMMENTS", "10"))


@dataclass(frozen=True)
class Settings:
    jira: JiraConfig = field(default_factory=JiraConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    app: AppConfig = field(default_factory=AppConfig)


# Module-level singleton
settings = Settings()
