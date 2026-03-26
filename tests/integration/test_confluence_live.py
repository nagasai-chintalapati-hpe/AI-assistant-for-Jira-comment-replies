"""Live integration tests for ConfluenceClient (skipped without credentials)."""

import os
import pytest
from dotenv import load_dotenv

load_dotenv(".env", override=True)

from src.integrations.confluence import ConfluenceClient

HAVE_CONFLUENCE_CREDS = bool(
    os.getenv("CONFLUENCE_BASE_URL")
    and os.getenv("CONFLUENCE_USERNAME")
    and os.getenv("CONFLUENCE_API_TOKEN")
)

pytestmark = pytest.mark.skipif(
    not HAVE_CONFLUENCE_CREDS,
    reason="Confluence credentials not configured in .env",
)

SPACE_KEY = os.getenv("CONFLUENCE_SPACES", "")
KNOWN_PAGE_ID = "4632839656"
KNOWN_PAGE_TITLE = "AI assistant for Jira comment replies Home"


@pytest.fixture(scope="module")
def confluence():
    """Module-scoped ConfluenceClient."""
    return ConfluenceClient()


class TestConfluenceConnection:
    """Verify basic connectivity."""

    def test_client_is_enabled(self, confluence):
        assert confluence.enabled is True

    def test_search_pages_in_space(self, confluence):
        pages = confluence.search_pages(space_key=SPACE_KEY)
        assert isinstance(pages, list)
        assert len(pages) >= 1

    def test_search_returns_known_page(self, confluence):
        pages = confluence.search_pages(space_key=SPACE_KEY)
        titles = [p["title"] for p in pages]
        assert KNOWN_PAGE_TITLE in titles


class TestConfluencePageRetrieval:
    """Verify page content retrieval."""

    def test_get_page_by_id(self, confluence):
        page = confluence.get_page(KNOWN_PAGE_ID)
        assert page is not None
        assert page.get("title") == KNOWN_PAGE_TITLE

    def test_get_page_content_as_text(self, confluence):
        text = confluence.get_page_content_as_text(KNOWN_PAGE_ID)
        assert text is not None
        assert isinstance(text, str)
        assert len(text) > 0

    def test_nonexistent_page_returns_none(self, confluence):
        page = confluence.get_page("99999999999")
        assert page is None


class TestConfluenceSpaceListing:
    """Verify space listing."""

    def test_get_all_pages_in_space(self, confluence):
        pages = confluence.get_all_pages_in_space(SPACE_KEY)
        assert isinstance(pages, list)
        assert len(pages) >= 1
        for p in pages:
            assert "id" in p
            assert "title" in p
