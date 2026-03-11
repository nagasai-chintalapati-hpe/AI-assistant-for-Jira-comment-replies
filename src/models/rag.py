"""RAG (Retrieval-Augmented Generation) data models.

Represents document chunks, retrieval snippets, and evidence
returned by the RAG engine for grounding draft responses.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DocumentChunk(BaseModel):
    """A chunk of a document stored in the vector index."""

    chunk_id: str
    source_type: str  # "confluence", "pdf", "runbook", "known_issue"
    source_title: str
    source_url: Optional[str] = None
    content: str
    metadata: Optional[dict[str, str]] = None  # component, version, env, etc.
    created_at: Optional[datetime] = None


class RAGSnippet(BaseModel):
    """A single retrieval result from the RAG index."""

    chunk_id: str
    source_type: str
    source_title: str
    source_url: Optional[str] = None
    content: str
    relevance_score: float  # 0.0 to 1.0 (higher = more relevant)
    metadata: Optional[dict[str, str]] = None


class RAGResult(BaseModel):
    """Aggregated RAG retrieval result for a query."""

    query: str
    snippets: list[RAGSnippet]
    total_chunks_searched: int = 0
    retrieval_duration_ms: float = 0.0


class LogEntry(BaseModel):
    """A single log entry retrieved by log lookup."""

    source: str  # "jenkins", "elk", "file"
    correlation_id: Optional[str] = None
    timestamp: Optional[str] = None
    level: Optional[str] = None  # ERROR, WARN, INFO, etc.
    message: str
    context: Optional[dict[str, str]] = None  # build_id, env, etc.
