"""Configuration management for Jira Comment Assistant (MVP v1).

Centralizes all environment-based configuration with validation and sensible defaults.
Loads from `.env` file at project root if present, otherwise uses environment variables.

Validation:
  - Required fields are checked on module load via validate() function
  - Invalid values raise ConfigurationError with clear messaging
  - Type coercion is explicit (int, float, bool)
  - Empty strings are treated as missing for required fields

Usage:
  from src.config import settings
  print(settings.jira.base_url)
  print(settings.app.log_level)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Setup logger for config validation
logger = logging.getLogger(__name__)

# Load .env from project root (if present)
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
    logger.info(f"Loaded environment from {_ENV_FILE}")


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""

    pass


@dataclass(frozen=True)
class JiraConfig:
    """Jira Cloud integration configuration.
    
    Required for production:
      - JIRA_BASE_URL: Jira instance URL (e.g., https://company.atlassian.net)
      - JIRA_USERNAME: API user email
      - JIRA_API_TOKEN: API token from account settings
    
    Raises ConfigurationError if required fields are missing in production.
    """

    base_url: str
    username: str
    api_token: str

    def __post_init__(self) -> None:
        """Validate Jira configuration."""
        env = os.getenv("ENV", "development").lower()
        
        if env == "production":
            if not self.base_url or not self.base_url.startswith("https://"):
                raise ConfigurationError(
                    "JIRA_BASE_URL is required and must be HTTPS in production"
                )
            if not self.username:
                raise ConfigurationError("JIRA_USERNAME is required in production")
            if not self.api_token:
                raise ConfigurationError("JIRA_API_TOKEN is required in production")


@dataclass(frozen=True)
class CopilotConfig:
    """LLM configuration (optional for MVP v1, required for refined drafts).
    
    If api_key is not set, system falls back to keyword-only mode.
    
    Attributes:
      api_key: LLM provider API key (optional)
      model: LLM model name (default: gpt-4)
      temperature: Sampling temperature 0.0-1.0 (default: 0.1 for consistency)
      max_tokens: Max output tokens (default: 1024)
    """

    api_key: str
    model: str
    temperature: float
    max_tokens: int

    def __post_init__(self) -> None:
        """Validate LLM configuration."""
        if not 0.0 <= self.temperature <= 1.0:
            raise ConfigurationError(
                f"COPILOT_TEMPERATURE must be between 0.0 and 1.0, got {self.temperature}"
            )
        if self.max_tokens < 128:
            raise ConfigurationError(
                f"COPILOT_MAX_TOKENS must be at least 128, got {self.max_tokens}"
            )


@dataclass(frozen=True)
class NotificationConfig:
    """Notification channels configuration (optional).
    
    Teams and Email are independent. If neither is configured, notifications
    are silently skipped without blocking the pipeline.
    
    Attributes:
      teams_webhook_url: Teams channel webhook URL (optional)
      smtp_host: SMTP server hostname (optional)
      smtp_port: SMTP port (default: 587)
      smtp_username: SMTP auth username (optional, required if smtp_host is set)
      smtp_password: SMTP auth password (optional, required if smtp_host is set)
      email_from: Sender email address (optional)
      email_to: Comma-separated recipient list (optional)
    """

    teams_webhook_url: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: str

    def __post_init__(self) -> None:
        """Validate notification configuration."""
        # Validate SMTP if host is set
        if self.smtp_host and not self.smtp_username:
            raise ConfigurationError("SMTP_USERNAME required when SMTP_HOST is configured")
        if self.smtp_host and not self.smtp_password:
            raise ConfigurationError("SMTP_PASSWORD required when SMTP_HOST is configured")
        if self.smtp_host and not self.email_from:
            raise ConfigurationError("EMAIL_FROM required when SMTP_HOST is configured")
        if self.smtp_port < 1 or self.smtp_port > 65535:
            raise ConfigurationError(
                f"SMTP_PORT must be 1-65535, got {self.smtp_port}"
            )


@dataclass(frozen=True)
class AppConfig:
    """Application server and runtime configuration.
    
    Attributes:
      host: Bind address (default: 0.0.0.0)
      port: Listen port (default: 8000)
      log_level: Logging level (default: INFO)
      max_comments: Max comments to retrieve per issue (default: 10)
    """

    host: str
    port: int
    log_level: str
    max_comments: int

    def __post_init__(self) -> None:
        """Validate application configuration."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigurationError(
                f"LOG_LEVEL must be one of {valid_levels}, got {self.log_level}"
            )
        if self.port < 1 or self.port > 65535:
            raise ConfigurationError(f"APP_PORT must be 1-65535, got {self.port}")
        if self.max_comments < 1:
            raise ConfigurationError(
                f"MAX_COMMENTS must be at least 1, got {self.max_comments}"
            )


@dataclass(frozen=True)
class Settings:
    """All configuration combined.
    
    Access via: settings.jira, settings.copilot, settings.notifications, settings.app
    """

    jira: JiraConfig
    copilot: CopilotConfig
    notifications: NotificationConfig
    app: AppConfig


def _load_settings() -> Settings:
    """Load and validate all settings from environment.
    
    Returns:
        Settings: Validated configuration object
        
    Raises:
        ConfigurationError: If any required field is invalid
    """
    try:
        jira = JiraConfig(
            base_url=os.getenv("JIRA_BASE_URL", ""),
            username=os.getenv("JIRA_USERNAME", ""),
            api_token=os.getenv("JIRA_API_TOKEN", ""),
        )
        
        copilot = CopilotConfig(
            api_key=os.getenv("COPILOT_API_KEY", ""),
            model=os.getenv("COPILOT_MODEL", "gpt-4"),
            temperature=float(os.getenv("COPILOT_TEMPERATURE", "0.1")),
            max_tokens=int(os.getenv("COPILOT_MAX_TOKENS", "1024")),
        )
        
        notifications = NotificationConfig(
            teams_webhook_url=os.getenv("TEAMS_WEBHOOK_URL", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            email_from=os.getenv("EMAIL_FROM", ""),
            email_to=os.getenv("EMAIL_TO", ""),
        )
        
        app = AppConfig(
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=int(os.getenv("APP_PORT", "8000")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            max_comments=int(os.getenv("MAX_COMMENTS", "10")),
        )
        
        return Settings(jira=jira, copilot=copilot, notifications=notifications, app=app)
    
    except ValueError as e:
        raise ConfigurationError(f"Invalid configuration value: {e}") from e
    except ConfigurationError:
        raise
    except Exception as e:
        raise ConfigurationError(f"Unexpected configuration error: {e}") from e


# Module-level singleton — validated on import
try:
    settings = _load_settings()
    logger.info("Configuration loaded and validated successfully")
