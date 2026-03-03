"""Configuration management for Jira Comment Assistant."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env from project root if present
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
    logger.info(f"Loaded environment from {_ENV_FILE}")


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""
    pass


@dataclass(frozen=True)
class JiraConfig:
    """Jira Cloud configuration."""
    
    base_url: str
    username: str
    api_token: str

    def __post_init__(self) -> None:
        """Validate Jira configuration."""
        env = os.getenv("ENV", "development").lower()
        if env == "production":
            if not self.base_url or not self.base_url.startswith("https://"):
                raise ConfigurationError("JIRA_BASE_URL required and must be HTTPS in production")
            if not self.username:
                raise ConfigurationError("JIRA_USERNAME required in production")
            if not self.api_token:
                raise ConfigurationError("JIRA_API_TOKEN required in production")


@dataclass(frozen=True)
class CopilotConfig:
    """GitHub Copilot SDK configuration."""
    
    api_key: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        """Validate Copilot configuration."""
        if not 0.0 <= self.temperature <= 1.0:
            raise ConfigurationError(f"COPILOT_TEMPERATURE must be 0.0-1.0, got {self.temperature}")
        if self.max_tokens < 128:
            raise ConfigurationError(f"COPILOT_MAX_TOKENS must be >= 128, got {self.max_tokens}")


@dataclass(frozen=True)
class NotificationConfig:
    """Notification channels configuration."""
    
    teams_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""

    def __post_init__(self) -> None:
        """Validate notification configuration."""
        if self.smtp_host and not self.smtp_username:
            raise ConfigurationError("SMTP_USERNAME required when SMTP_HOST is set")
        if self.smtp_host and not self.smtp_password:
            raise ConfigurationError("SMTP_PASSWORD required when SMTP_HOST is set")
        if self.smtp_port < 1 or self.smtp_port > 65535:
            raise ConfigurationError(f"SMTP_PORT must be 1-65535, got {self.smtp_port}")


@dataclass(frozen=True)
class AppConfig:
    """Application server configuration."""
    
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    max_comments: int = 10

    def __post_init__(self) -> None:
        """Validate app configuration."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigurationError(f"LOG_LEVEL must be one of {valid_levels}, got {self.log_level}")
        if self.port < 1 or self.port > 65535:
            raise ConfigurationError(f"APP_PORT must be 1-65535, got {self.port}")
        if self.max_comments < 1:
            raise ConfigurationError(f"MAX_COMMENTS must be >= 1, got {self.max_comments}")


@dataclass(frozen=True)
class Settings:
    """All configuration combined."""
    
    jira: JiraConfig
    copilot: CopilotConfig
    notifications: NotificationConfig
    app: AppConfig


def _load_settings() -> Settings:
    """Load and validate all settings from environment."""
    try:
        jira = JiraConfig(
            base_url=os.getenv("JIRA_BASE_URL", ""),
            username=os.getenv("JIRA_USERNAME", ""),
            api_token=os.getenv("JIRA_API_TOKEN", ""),
        )
        
        copilot = CopilotConfig(
            api_key=os.getenv("COPILOT_API_KEY", ""),
            model=os.getenv("COPILOT_MODEL", "claude-sonnet-4.5"),
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

try:
    settings = _load_settings()
    logger.info("Configuration loaded and validated")
except ConfigurationError as e:
    logger.error("Configuration error: %s", e)
    raise
