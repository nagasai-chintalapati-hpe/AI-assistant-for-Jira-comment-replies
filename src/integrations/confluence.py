"""Confluence Cloud integration — search, content extraction, citations."""

from __future__ import annotations

import html
import io
import logging
import re
from dataclasses import dataclass
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


class ConfluenceClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
    ) -> None:
        self._base_url = base_url or settings.confluence.base_url
        self._username = username or settings.confluence.username
        self._api_token = api_token or settings.confluence.api_token
        self._client = None
        self._spaces = [
            s.strip() for s in (settings.confluence.spaces or "").split(",") if s.strip()
        ]
        self._labels = [
            l.strip() for l in (settings.confluence.labels or "").split(",") if l.strip()
        ]

        if self._base_url and self._username and self._api_token:
            try:
                from atlassian import Confluence

                self._client = Confluence(
                    url=self._base_url,
                    username=self._username,
                    password=self._api_token,
                    cloud=True,
                )
                logger.info("Confluence client initialised (%s)", self._base_url)
            except Exception as exc:
                logger.warning("Could not initialise Confluence client: %s", exc)
        else:
            logger.info("Confluence client not configured (missing credentials)")

    @property
    def enabled(self) -> bool:
        """Whether the Confluence client is properly configured."""
        return self._client is not None

    # Page retrieval

    def get_page(self, page_id: str) -> Optional[dict]:
        """Fetch a Confluence page by ID."""
        if not self._client:
            logger.warning("Confluence not configured — cannot fetch page %s", page_id)
            return None

        try:
            page = self._client.get_page_by_id(
                page_id,
                expand="body.storage,space,version",
            )
            return page
        except Exception as exc:
            logger.error("Failed to fetch Confluence page %s: %s", page_id, exc)
            return None

    def get_page_content_as_text(self, page_id: str) -> Optional[str]:
        page = self.get_page(page_id)
        if not page:
            return None

        body = page.get("body", {}).get("storage", {}).get("value", "")
        if not body:
            return None

        return self._html_to_text(body)

    # Search

    def search_pages(
        self,
        space_key: Optional[str] = None,
        label: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        if not self._client:
            logger.warning("Confluence not configured — cannot search")
            return []

        cql_parts: list[str] = ["type=page"]
        if space_key:
            cql_parts.append(f'space="{space_key}"')
        if label:
            cql_parts.append(f'label="{label}"')

        cql = " AND ".join(cql_parts)

        try:
            results = self._client.cql(cql, limit=limit)
            pages: list[dict] = []
            for item in results.get("results", []):
                content = item.get("content", item)
                pages.append({
                    "id": str(content.get("id", "")),
                    "title": content.get("title", ""),
                    "space": content.get("space", {}).get("key", ""),
                    "url": self._build_page_url(content),
                })
            return pages
        except Exception as exc:
            logger.error("Confluence search failed (CQL=%s): %s", cql, exc)
            return []

    def get_all_pages_in_space(
        self,
        space_key: str,
        limit: int = 100,
    ) -> list[dict]:
        """Get all pages in a Confluence space."""
        return self.search_pages(space_key=space_key, limit=limit)

    # Helpers

    @staticmethod
    def _html_to_text(html_content: str) -> str:
        # Remove script and style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)

        # Replace block-level tags with newlines
        text = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)

        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)

        # Decode HTML entities
        text = html.unescape(text)

        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()

    def _build_page_url(self, content: dict) -> str:
        """Build a full URL for a Confluence page."""
        page_id = content.get("id", "")
        space_key = content.get("space", {}).get("key", "")
        base = self._base_url.rstrip("/")
        if base and space_key and page_id:
            return f"{base}/wiki/spaces/{space_key}/pages/{page_id}"
        return ""

    #  Ground-truth retrieval 

    def search_by_text(
        self,
        query: str,
        space_key: Optional[str] = None,
        label: Optional[str] = None,
        limit: int = 10,
        excerpt_length: int = 300,
    ) -> list["ConfluenceCitation"]:
        if not self._client:
            logger.warning("Confluence not configured — cannot search")
            return []

        cql_parts: list[str] = ["type=page", f'text ~ "{query}"']
        if space_key:
            cql_parts.append(f'space="{space_key}"')
        elif self._spaces:
            space_list = " OR ".join(f'space="{s}"' for s in self._spaces)
            cql_parts.append(f"({space_list})")
        if label:
            cql_parts.append(f'label="{label}"')
        elif self._labels:
            label_list = " OR ".join(f'label="{l}"' for l in self._labels)
            cql_parts.append(f"({label_list})")

        cql = " AND ".join(cql_parts)

        try:
            results = self._client.cql(
                cql, limit=limit, expand="content.body.storage,content.space"
            )
            citations: list[ConfluenceCitation] = []
            for item in results.get("results", []):
                content = item.get("content", item)
                page_id = str(content.get("id", ""))
                title = content.get("title", "")
                space = content.get("space", {}).get("key", "")
                url = self._build_page_url(content)

                # Extract a plain-text excerpt from the body
                body_html = (
                    content.get("body", {})
                    .get("storage", {})
                    .get("value", "")
                )
                full_text = self._html_to_text(body_html) if body_html else ""
                excerpt = full_text[:excerpt_length].strip()

                citations.append(
                    ConfluenceCitation(
                        page_id=page_id,
                        title=title,
                        space_key=space,
                        url=url,
                        excerpt=excerpt,
                        full_text=full_text,
                    )
                )
            return citations
        except Exception as exc:
            logger.error("Confluence text search failed (CQL=%s): %s", cql, exc)
            return []

    def get_page_with_citation(
        self,
        page_id: str,
        excerpt_length: int = 300,
    ) -> Optional["ConfluenceCitation"]:
        """Fetch a Confluence page and return it as a citation."""
        page = self.get_page(page_id)
        if not page:
            return None

        body_html = page.get("body", {}).get("storage", {}).get("value", "")
        full_text = self._html_to_text(body_html) if body_html else ""
        space_key = page.get("space", {}).get("key", "")
        url = self._build_page_url(page)

        return ConfluenceCitation(
            page_id=page_id,
            title=page.get("title", ""),
            space_key=space_key,
            url=url,
            excerpt=full_text[:excerpt_length].strip(),
            full_text=full_text,
        )

    def fetch_pdf_attachment(
        self,
        page_id: str,
        filename: Optional[str] = None,
    ) -> Optional["ConfluenceCitation"]:
        if not self._client:
            logger.warning("Confluence not configured — cannot fetch PDF")
            return None

        try:
            attachments = self._client.get_attachments_from_content(page_id)
            pdf_attachments = [
                a for a in (attachments.get("results") or [])
                if (a.get("title") or "").lower().endswith(".pdf")
            ]
            if filename:
                pdf_attachments = [
                    a for a in pdf_attachments
                    if a.get("title", "").lower() == filename.lower()
                ]

            if not pdf_attachments:
                logger.info("No PDF attachment found on page %s", page_id)
                return None

            att = pdf_attachments[0]
            att_title = att.get("title", "attachment.pdf")
            download_url = att.get("_links", {}).get("download", "")
            if not download_url:
                return None

            # Download the PDF bytes
            full_url = f"{self._base_url.rstrip('/')}/wiki{download_url}"
            import requests as _requests

            resp = _requests.get(
                full_url,
                auth=(self._username, self._api_token),
                timeout=30,
            )
            resp.raise_for_status()
            pdf_bytes = resp.content

            # Extract text
            text = self._extract_pdf_text(pdf_bytes)
            if not text:
                logger.warning("PDF text extraction returned empty for %s", att_title)
                return None

            page = self.get_page(page_id)
            page_title = (page or {}).get("title", page_id)
            space_key = (page or {}).get("space", {}).get("key", "")
            url = self._build_page_url(page) if page else ""

            return ConfluenceCitation(
                page_id=page_id,
                title=f"{page_title} — {att_title}",
                space_key=space_key,
                url=url,
                excerpt=text[:300].strip(),
                full_text=text,
                source_type="pdf",
            )
        except Exception as exc:
            logger.error("PDF attachment fetch failed for page %s: %s", page_id, exc)
            return None

    @staticmethod
    def _extract_pdf_text(pdf_bytes: bytes) -> str:
        """Extract plain text from PDF bytes. Tries PyPDF2 then pdfplumber."""
        # Attempt 1: PyPDF2
        try:
            from PyPDF2 import PdfReader  # type: ignore[import]

            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages_text = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(pages_text).strip()
            if text:
                return text
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("PyPDF2 extraction failed: %s", exc)

        # Attempt 2: pdfplumber
        try:
            import pdfplumber  # type: ignore[import]

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages_text = [p.extract_text() or "" for p in pdf.pages]
            text = "\n\n".join(pages_text).strip()
            if text:
                return text
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("pdfplumber extraction failed: %s", exc)

        logger.warning("No PDF extraction library available (install PyPDF2 or pdfplumber)")
        return ""


@dataclass
class ConfluenceCitation:
    """A citable piece of evidence from Confluence."""

    page_id: str
    title: str
    space_key: str
    url: str
    excerpt: str           # first N chars of plain text
    full_text: str = ""    # complete plain text (for RAG ingestion)
    source_type: str = "confluence"  # "confluence" | "pdf"

    def to_citation_dict(self) -> dict[str, str]:
        """Return a dict matching the draft citation schema."""
        return {
            "source": f"Confluence: {self.title}",
            "url": self.url,
            "excerpt": self.excerpt[:200],
            "type": self.source_type,
        }
