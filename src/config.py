"""Configuration management for Jira Comment Assistant."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

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
class LLMConfig:
    """Local LLM configuration (llama.cpp / GGUF)."""

    backend: str = os.getenv("LLM_BACKEND", "copilot")  # "local" or "copilot"
    model_path: str = os.getenv("LLM_MODEL_PATH", "")  # path to .gguf file
    n_ctx: int = int(os.getenv("LLM_N_CTX", "4096"))  # context window
    n_gpu_layers: int = int(os.getenv("LLM_N_GPU_LAYERS", "0"))  # 0 = CPU only
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "1024"))
    n_threads: int = int(os.getenv("LLM_N_THREADS", "4"))


@dataclass(frozen=True)
class RAGConfig:
    """RAG pipeline configuration."""

    chroma_persist_dir: str = os.getenv("CHROMA_PERSIST_DIR", ".data/chroma")
    embedding_model: str = os.getenv("RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    chunk_size: int = int(os.getenv("RAG_CHUNK_SIZE", "500"))
    chunk_overlap: int = int(os.getenv("RAG_CHUNK_OVERLAP", "50"))
    top_k: int = int(os.getenv("RAG_TOP_K", "5"))
    pdf_upload_dir: str = os.getenv("PDF_UPLOAD_DIR", ".data/pdfs")


@dataclass(frozen=True)
class ConfluenceConfig:
    """Confluence integration for RAG ingestion."""

    base_url: str = os.getenv("CONFLUENCE_BASE_URL", "")
    username: str = os.getenv("CONFLUENCE_USERNAME", "")
    api_token: str = os.getenv("CONFLUENCE_API_TOKEN", "")
    spaces: str = os.getenv("CONFLUENCE_SPACES", "")  # comma-separated space keys
    labels: str = os.getenv("CONFLUENCE_LABELS", "")  # comma-separated labels


@dataclass(frozen=True)
class TestRailConfig:
    """TestRail integration configuration.

    Auth priority:
      1. API key (production — stateless, no expiry)
      2. Session cookie (dev/testing — fallback for SSO instances)
    """

    base_url: str = os.getenv("TESTRAIL_BASE_URL", "")
    username: str = os.getenv("TESTRAIL_USERNAME", "")
    api_key: str = os.getenv("TESTRAIL_API_KEY", "")
    session_cookie: str = os.getenv("TESTRAIL_SESSION_COOKIE", "")
    project_id: int = int(os.getenv("TESTRAIL_PROJECT_ID", "0"))
    suite_id: int = int(os.getenv("TESTRAIL_SUITE_ID", "0"))


@dataclass(frozen=True)
class LogLookupConfig:
    """Log lookup service configuration."""

    jenkins_base_url: str = os.getenv("JENKINS_BASE_URL", "")
    jenkins_username: str = os.getenv("JENKINS_USERNAME", "")
    jenkins_api_token: str = os.getenv("JENKINS_API_TOKEN", "")
    log_dir: str = os.getenv("LOG_DIR", "")  # local log directory
    default_time_window_hours: int = int(os.getenv("LOG_TIME_WINDOW_HOURS", "24"))


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
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "8000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    max_comments: int = int(os.getenv("MAX_COMMENTS", "10"))
    db_path: str = os.getenv("ASSISTANT_DB_PATH", ".data/assistant.db")


@dataclass(frozen=True)
class Settings:
    jira: JiraConfig = field(default_factory=JiraConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    confluence: ConfluenceConfig = field(default_factory=ConfluenceConfig)
    testrail: TestRailConfig = field(default_factory=TestRailConfig)
    log_lookup: LogLookupConfig = field(default_factory=LogLookupConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    app: AppConfig = field(default_factory=AppConfig)


# Module-level singleton
settings = Settings()
