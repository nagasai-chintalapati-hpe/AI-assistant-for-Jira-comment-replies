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
    draft_field_id: str = os.getenv("JIRA_DRAFT_FIELD_ID", "")  # e.g. customfield_10200


@dataclass(frozen=True)
class CopilotConfig:
    api_key: str = os.getenv("COPILOT_API_KEY", "")
    base_url: str = os.getenv("COPILOT_BASE_URL", "https://api.githubcopilot.com")
    model: str = os.getenv("COPILOT_MODEL", "gpt-4o")
    temperature: float = float(os.getenv("COPILOT_TEMPERATURE", "0.1"))
    max_tokens: int = int(os.getenv("COPILOT_MAX_TOKENS", "1024"))


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
class GitConfig:
    """Git provider integration (GitHub / GitLab / Bitbucket)."""

    provider: str = os.getenv("GIT_PROVIDER", "github")  # github | gitlab | bitbucket
    base_url: str = os.getenv("GIT_BASE_URL", "https://api.github.com")  # override for self-hosted
    token: str = os.getenv("GIT_TOKEN", "")
    owner: str = os.getenv("GIT_OWNER", "")  # org/user
    repo: str = os.getenv("GIT_REPO", "")   # default repo (optional)


@dataclass(frozen=True)
class ELKConfig:
    """Elasticsearch / OpenSearch log query configuration."""

    host: str = os.getenv("ELK_HOST", "")          # e.g. https://elk.internal:9200
    username: str = os.getenv("ELK_USERNAME", "")
    password: str = os.getenv("ELK_PASSWORD", "")
    api_key: str = os.getenv("ELK_API_KEY", "")    # alternative to user/pass
    index_pattern: str = os.getenv("ELK_INDEX_PATTERN", "logs-*")  # index alias / pattern
    default_time_window_hours: int = int(os.getenv("ELK_TIME_WINDOW_HOURS", "24"))
    max_hits: int = int(os.getenv("ELK_MAX_HITS", "50"))


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
class WebhookConfig:
    """Jira webhook HMAC-SHA256 signature validation."""

    secret: str = os.getenv("JIRA_WEBHOOK_SECRET", "")
    # Set VALIDATE_WEBHOOK_SIGNATURE=true in production once the Jira webhook
    # secret is configured.  Disabled by default for easier local dev.
    validate_signature: bool = (
        os.getenv("VALIDATE_WEBHOOK_SIGNATURE", "false").lower() == "true"
    )


@dataclass(frozen=True)
class RateLimitConfig:
    """Per-IP rate limiting on the webhook endpoint."""

    enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    max_requests_per_minute: int = int(os.getenv("RATE_LIMIT_RPM", "60"))


@dataclass(frozen=True)
class S3Config:
    """S3 / MinIO artifact storage configuration."""

    bucket: str = os.getenv("S3_BUCKET", "")
    endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "")  # empty = AWS default
    access_key: str = os.getenv("S3_ACCESS_KEY", "")
    secret_key: str = os.getenv("S3_SECRET_KEY", "")
    region: str = os.getenv("S3_REGION", "us-east-1")
    artifacts_prefix: str = os.getenv("S3_ARTIFACTS_PREFIX", "artifacts/")


@dataclass(frozen=True)
class RedisConfig:
    """Redis configuration for distributed rate limiting and caching."""

    enabled: bool = os.getenv("REDIS_ENABLED", "false").lower() == "true"
    host: str = os.getenv("REDIS_HOST", "localhost")
    port: int = int(os.getenv("REDIS_PORT", "6379"))
    password: str = os.getenv("REDIS_PASSWORD", "")
    db: int = int(os.getenv("REDIS_DB", "0"))
    # Full URL overrides host/port/password when set
    url: str = os.getenv("REDIS_URL", "")


@dataclass(frozen=True)
class QueueConfig:
    """RabbitMQ / AMQP message queue configuration."""

    enabled: bool = os.getenv("QUEUE_ENABLED", "false").lower() == "true"
    url: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/")
    queue_name: str = os.getenv("QUEUE_NAME", "jira_webhook_events")
    prefetch_count: int = int(os.getenv("QUEUE_PREFETCH_COUNT", "1"))


@dataclass(frozen=True)
class AppConfig:
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "8000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    max_comments: int = int(os.getenv("MAX_COMMENTS", "10"))
    db_path: str = os.getenv("ASSISTANT_DB_PATH", ".data/assistant.db")
    # Public base URL of this service — used for Review UI links in Teams cards
    base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000")


@dataclass(frozen=True)
class Settings:
    jira: JiraConfig = field(default_factory=JiraConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    confluence: ConfluenceConfig = field(default_factory=ConfluenceConfig)
    testrail: TestRailConfig = field(default_factory=TestRailConfig)
    log_lookup: LogLookupConfig = field(default_factory=LogLookupConfig)
    git: GitConfig = field(default_factory=GitConfig)
    elk: ELKConfig = field(default_factory=ELKConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    app: AppConfig = field(default_factory=AppConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    s3: S3Config = field(default_factory=S3Config)
    redis: RedisConfig = field(default_factory=RedisConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)


# Module-level singleton
settings = Settings()
