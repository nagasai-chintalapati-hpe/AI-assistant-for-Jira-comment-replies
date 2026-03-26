"""Shared singletons for the API."""

from __future__ import annotations

import logging
import os
from typing import Optional

from src.api.security import _RateLimiter
from src.config import settings
from src.storage.sqlite_store import SQLiteDraftStore, SQLiteIdempotencyStore
from src.api.event_filter import EventFilter
from src.agent.classifier import CommentClassifier
from src.agent.drafter import ResponseDrafter
from src.integrations.notifications import (
    TeamsNotifier,
    EmailNotifier,
    NotificationService,
)
from src.integrations.log_lookup import LogLookupService
from src.integrations.testrail import TestRailClient
from src.integrations.jira import JiraClient
from src.integrations.git import GitClient
from src.integrations.s3_connector import S3ArtifactFetcher
from src.integrations.jenkins import JenkinsClient
from src.integrations.confluence import ConfluenceClient
from src.queue.broker import MessageBroker
from src.llm.client import get_llm_client

logger = logging.getLogger(__name__)

#  Persistent stores 

draft_store = SQLiteDraftStore(db_path=settings.app.db_path)
_idempotency_store = SQLiteIdempotencyStore(db_path=settings.app.db_path)
event_filter = EventFilter(idempotency_store=_idempotency_store)

#  LLM / Classifier / Drafter 

_llm_client = get_llm_client()
classifier = CommentClassifier(llm_client=_llm_client)
drafter = ResponseDrafter(llm_client=_llm_client)

# Tooling layer connectors (each degrades gracefully when unconfigured) ─

_log_lookup = LogLookupService()
_testrail_client = TestRailClient()

_jira_client: Optional[JiraClient] = None
try:
    _jira_client = JiraClient()
except Exception:
    _jira_client = None

_git_client: Optional[GitClient] = None
try:
    _git_client = GitClient()
    if not _git_client.enabled:
        _git_client = None
except Exception:
    _git_client = None

_s3_fetcher = S3ArtifactFetcher()
_jenkins_client = JenkinsClient()

_confluence_client: Optional[ConfluenceClient] = None
try:
    _confluence_client = ConfluenceClient()
    if not _confluence_client.enabled:
        _confluence_client = None
except Exception:
    _confluence_client = None

_broker = MessageBroker()

# Rate limiter (per IP, Redis-backed or in-memory) 

_rate_limiter = _RateLimiter(rpm=settings.rate_limit.max_requests_per_minute)

#  Notification channels (Teams Email) 

_teams = TeamsNotifier(webhook_url=os.getenv("TEAMS_WEBHOOK_URL"))
_email = EmailNotifier(
    smtp_host=os.getenv("SMTP_HOST"),
    smtp_port=int(os.getenv("SMTP_PORT", "587")),
    smtp_username=os.getenv("SMTP_USERNAME"),
    smtp_password=os.getenv("SMTP_PASSWORD"),
    from_address=os.getenv("EMAIL_FROM"),
    to_addresses=[
        a.strip()
        for a in (os.getenv("EMAIL_TO") or "").split(",")
        if a.strip()
    ],
)
notifier = NotificationService(teams=_teams, email=_email)

#  RAG pipeline 

_rag_engine = None
_rag_ingester = None


def _get_rag_engine():
    """Lazy-init the RAG engine singleton."""
    global _rag_engine
    if _rag_engine is None:
        from src.rag.engine import RAGEngine
        _rag_engine = RAGEngine()
    return _rag_engine


def _get_rag_ingester():
    """Lazy-init the document ingester singleton."""
    global _rag_ingester
    if _rag_ingester is None:
        from src.rag.ingest import DocumentIngester
        _rag_ingester = DocumentIngester(_get_rag_engine())
    return _rag_ingester
