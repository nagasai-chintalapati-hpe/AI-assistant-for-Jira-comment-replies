"""Document ingestion pipeline — PDF, Confluence, and raw text."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from src.config import settings
from src.models.rag import DocumentChunk
from src.rag.engine import RAGEngine

logger = logging.getLogger(__name__)


class DocumentIngester:
    """Extracts, chunks, and indexes documents into the RAG engine. """

    def __init__(
        self,
        rag_engine: RAGEngine,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
    ) -> None:
        self._engine = rag_engine
        self._chunk_size = chunk_size or settings.rag.chunk_size
        self._chunk_overlap = chunk_overlap or settings.rag.chunk_overlap

    # Public API

    def ingest_pdf(
        self,
        file_path: str,
        source_title: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> int:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {file_path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a .pdf file, got: {path.suffix}")

        title = source_title or path.stem

        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            pages_text: list[str] = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text.strip())

            full_text = "\n\n".join(pages_text)
        except Exception as exc:
            logger.error("Failed to parse PDF %s: %s", file_path, exc)
            raise

        if not full_text.strip():
            logger.warning("PDF %s produced no extractable text", file_path)
            return 0

        return self.ingest_text(
            text=full_text,
            source_title=title,
            source_type="pdf",
            source_url=None,
            metadata=metadata,
        )

    def ingest_jira_resolved(
        self,
        jira_client,
        project_keys: Optional[list[str]] = None,
        max_issues: int = 100,
        statuses: Optional[list[str]] = None,
    ) -> int:
        """Ingest resolved Jira tickets as prior-defect context for RAG. """
        resolved_statuses = statuses or ["Done", "Resolved", "Closed"]
        status_jql = " OR ".join(f'status = "{s}"' for s in resolved_statuses)

        jql_parts = ["issuetype in (Bug, Defect)", f"({status_jql})"]
        if project_keys:
            project_jql = " OR ".join(f'project = "{k}"' for k in project_keys)
            jql_parts.append(f"({project_jql})")
        jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"

        issues = jira_client.search_issues(jql=jql, max_results=max_issues)

        total_chunks = 0
        for issue in issues:
            fields = issue.get("fields", {})
            issue_key = issue.get("key", "")
            summary = fields.get("summary", "") or ""
            description = fields.get("description", "") or ""
            resolution = (fields.get("resolution") or {}).get("name", "")

            # Last comment often contains the resolution details
            comments = fields.get("comment", {}).get("comments", [])
            last_comment_body = comments[-1].get("body", "") if comments else ""

            text_parts = [f"Issue: {issue_key}", f"Summary: {summary}"]
            if description:
                text_parts.append(f"Description: {description[:1000]}")
            if resolution:
                text_parts.append(f"Resolution: {resolution}")
            if last_comment_body:
                text_parts.append(f"Resolution note: {last_comment_body[:500]}")

            text = "\n\n".join(text_parts)
            jira_url: Optional[str] = None
            try:
                jira_url = f"{settings.jira.base_url.rstrip('/')}/browse/{issue_key}"
            except Exception:
                pass

            count = self.ingest_text(
                text=text,
                source_title=f"{issue_key}: {summary[:80]}",
                source_type="jira",
                source_url=jira_url,
                metadata={
                    "issue_key": issue_key,
                    "status": resolution or "resolved",
                    "source": "jira",
                },
            )
            total_chunks += count

        logger.info(
            "Ingested %d chunks from %d resolved Jira issues",
            total_chunks,
            len(issues),
        )
        return total_chunks

    def ingest_text(
        self,
        text: str,
        source_title: str,
        source_type: str = "text",
        source_url: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> int:
        """Chunk raw text and ingest into the RAG index."""
        if not text.strip():
            return 0

        raw_chunks = self.chunk_text(text)
        if not raw_chunks:
            return 0

        doc_chunks: list[DocumentChunk] = []
        for idx, chunk_text in enumerate(raw_chunks):
            chunk_id = RAGEngine.generate_chunk_id(source_title, idx)
            doc_chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    source_type=source_type,
                    source_title=source_title,
                    source_url=source_url,
                    content=chunk_text,
                    metadata=metadata,
                )
            )

        count = self._engine.add_chunks(doc_chunks)
        logger.info(
            "Ingested %d chunks from '%s' (type=%s)",
            count, source_title, source_type,
        )
        return count

    def ingest_confluence_page(
        self,
        page_id: str,
        confluence_client=None,
    ) -> int:
        """Fetch a Confluence page and ingest its text content. """
        if confluence_client is None:
            from src.integrations.confluence import ConfluenceClient
            confluence_client = ConfluenceClient()

        page_data = confluence_client.get_page(page_id)
        if not page_data:
            logger.warning("Confluence page %s not found or empty", page_id)
            return 0

        title = page_data.get("title", f"confluence-{page_id}")
        content = confluence_client.get_page_content_as_text(page_id)
        if not content:
            logger.warning("Confluence page %s has no text content", page_id)
            return 0

        base_url = settings.confluence.base_url.rstrip("/")
        space_key = page_data.get("space", {}).get("key", "")
        page_url = f"{base_url}/wiki/spaces/{space_key}/pages/{page_id}" if base_url else None

        return self.ingest_text(
            text=content,
            source_title=title,
            source_type="confluence",
            source_url=page_url,
            metadata={"page_id": page_id, "space_key": space_key},
        )

    # Chunking

    def chunk_text(self, text: str) -> list[str]:
        """Split *text* into overlapping chunks.  """
        if not text.strip():
            return []

        size = self._chunk_size
        overlap = self._chunk_overlap

        # Ensure overlap is smaller than size
        if overlap >= size:
            overlap = size // 4

        chunks: list[str] = []
        start = 0
        text_len = len(text)

        while start < text_len:
            end = min(start + size, text_len)
            chunk = text[start:end]

            # Try to break at paragraph boundary
            if end < text_len:
                last_para = chunk.rfind("\n\n")
                if last_para > size // 2:
                    end = start + last_para + 2
                    chunk = text[start:end]
                else:
                    # Try sentence boundary
                    for sep in (". ", ".\n", "! ", "? "):
                        last_sent = chunk.rfind(sep)
                        if last_sent > size // 2:
                            end = start + last_sent + len(sep)
                            chunk = text[start:end]
                            break

            chunk = chunk.strip()
            if chunk:
                chunks.append(chunk)

            # Advance with overlap
            if end >= text_len:
                break
            start = end - overlap

        return chunks

    # Helpers

    @staticmethod
    def extract_pdf_text(file_path: str) -> str:
        """Extract all text from a PDF file."""
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        return "\n\n".join(pages)
