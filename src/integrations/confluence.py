"""Confluence Cloud integration for RAG ingestion.

Wraps the ``atlassian-python-api`` Confluence client to fetch page
content as plain text for chunking and indexing.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


class ConfluenceClient:
    """Thin wrapper around the Atlassian Confluence REST API.

    Requires ``CONFLUENCE_BASE_URL``, ``CONFLUENCE_USERNAME``, and
    ``CONFLUENCE_API_TOKEN`` environment variables to be set.
    """

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
        """Fetch a Confluence page by ID.

        Returns the page data dict or None if not found / not configured.
        """
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
        """Extract plain text from a Confluence page's storage body.

        Strips HTML tags and decodes entities to produce clean text
        suitable for chunking.

        Returns None if the page doesn't exist or has no content.
        """
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
        """Search Confluence pages by space and/or label.

        Returns a list of page summary dicts with ``id``, ``title``,
        ``space``, and ``url`` keys.
        """
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
        """Get all pages in a Confluence space.

        Returns a list of page summary dicts.
        """
        return self.search_pages(space_key=space_key, limit=limit)

    # Helpers

    @staticmethod
    def _html_to_text(html_content: str) -> str:
        """Convert HTML storage format to plain text.

        Simple regex-based approach — strips tags, collapses whitespace,
        and decodes HTML entities.
        """
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
