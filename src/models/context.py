"""Context collection data models.

Captures all issue metadata, comment thread, attachments,
Jenkins console-log links, RAG snippets, and log entries
gathered by the ContextCollector.
"""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel

from src.models.rag import RAGSnippet, LogEntry


class CommentSnapshot(BaseModel):
    """Lightweight snapshot of a single Jira comment."""

    comment_id: str
    author: str
    author_role: Optional[str] = None
    created: str
    body: str


class IssueContext(BaseModel):
    """Represents collected context for a Jira issue"""

    issue_key: str
    summary: str
    description: str
    issue_type: str
    status: str
    priority: str
    environment: Optional[str] = None
    versions: Optional[list[str]] = None
    components: Optional[list[str]] = None
    labels: Optional[list[str]] = None

    # Relations
    linked_issues: Optional[list[dict[str, str]]] = None  # {key, type, status}
    attached_files: Optional[list[dict[str, Any]]] = None  # {name, url, type}

    # Comment thread (last N)
    last_comments: Optional[list[CommentSnapshot]] = None

    # History
    changelog: Optional[list[dict[str, Any]]] = None
    comment_count: int = 0


class ContextCollectionResult(BaseModel):
    """Result of context collection with source tracking"""

    issue_context: IssueContext
    jenkins_links: Optional[list[str]] = None

    rag_snippets: Optional[list[RAGSnippet]] = None
    log_entries: Optional[list[LogEntry]] = None
    testrail_results: Optional[list[dict[str, Any]]] = None
    build_metadata: Optional[dict[str, str]] = None  # commit, version, deploy_ts

    collection_timestamp: datetime
    collection_duration_ms: float
