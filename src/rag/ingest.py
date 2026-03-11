"""Document ingestion pipeline — PDF, Confluence, and raw text.

Handles text extraction, chunking, and indexing into the RAG engine.
Chunking uses a sliding-window approach with configurable size and
overlap (from ``RAGConfig``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from src.config import settings
from src.models.rag import DocumentChunk
from src.rag.engine import RAGEngine

logger = logging.getLogger(__name__)


class DocumentIngester:
    """Extracts, chunks, and indexes documents into the RAG engine.

    Parameters
    ----------
    rag_engine : RAGEngine
        The RAG engine to ingest chunks into.
    chunk_size : int | None
        Character-level chunk size.  Defaults to ``settings.rag.chunk_size``.
    chunk_overlap : int | None
        Overlap between consecutive chunks.  Defaults to
        ``settings.rag.chunk_overlap``.
    """

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
        """Parse a PDF file and ingest its text into the RAG index.

        Parameters
        ----------
        file_path : str
            Path to the ``.pdf`` file.
        source_title : str | None
            Human-readable document title.  Defaults to the filename.
        metadata : dict | None
            Extra metadata to attach to every chunk (component, version, …).

        Returns
        -------
        int
            Number of chunks indexed.
        """
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

    def ingest_text(
        self,
        text: str,
        source_title: str,
        source_type: str = "text",
        source_url: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> int:
        """Chunk raw text and ingest into the RAG index.

        Parameters
        ----------
        text : str
            Full document text.
        source_title : str
            Human-readable title for the document.
        source_type : str
            Category — ``"pdf"``, ``"confluence"``, ``"runbook"``,
            ``"known_issue"``, etc.
        source_url : str | None
            Optional URL back to the original document.
        metadata : dict | None
            Extra metadata to attach to every chunk.

        Returns
        -------
        int
            Number of chunks indexed.
        """
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
        """Fetch a Confluence page and ingest its text content.

        Parameters
        ----------
        page_id : str
            Confluence page ID.
        confluence_client : ConfluenceClient | None
            Confluence client to use.  If None, a new one is created from
            config.

        Returns
        -------
        int
            Number of chunks indexed.
        """
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
        """Split *text* into overlapping chunks.

        Uses a sliding window of ``chunk_size`` characters with
        ``chunk_overlap`` overlap.  Tries to break at paragraph or
        sentence boundaries when possible.

        Returns
        -------
        list[str]
            Ordered list of text chunks.
        """
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
        """Extract all text from a PDF file (utility method)."""
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        return "\n\n".join(pages)
