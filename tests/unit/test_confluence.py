"""Tests for the Confluence client integration."""

import pytest
from unittest.mock import patch, MagicMock

from src.integrations.confluence import ConfluenceClient


# Fixtures

@pytest.fixture
def confluence_client():
    """Create a ConfluenceClient with a mocked Atlassian client."""
    with patch("src.integrations.confluence.settings") as mock_settings:
        mock_settings.confluence.base_url = "https://wiki.example.com"
        mock_settings.confluence.username = "user@example.com"
        mock_settings.confluence.api_token = "token"

        client = ConfluenceClient.__new__(ConfluenceClient)
        client._base_url = "https://wiki.example.com"
        client._username = "user@example.com"
        client._api_token = "token"
        client._client = MagicMock()

        yield client


@pytest.fixture
def unconfigured_client():
    """Create a ConfluenceClient with no credentials."""
    with patch("src.integrations.confluence.settings") as mock_settings:
        mock_settings.confluence.base_url = ""
        mock_settings.confluence.username = ""
        mock_settings.confluence.api_token = ""

        client = ConfluenceClient.__new__(ConfluenceClient)
        client._base_url = ""
        client._username = ""
        client._api_token = ""
        client._client = None

        yield client


# Tests — enabled

class TestConfluenceEnabled:
    def test_enabled_when_configured(self, confluence_client):
        assert confluence_client.enabled is True

    def test_disabled_when_unconfigured(self, unconfigured_client):
        assert unconfigured_client.enabled is False


# Tests — get_page

class TestGetPage:
    def test_get_page_success(self, confluence_client):
        confluence_client._client.get_page_by_id.return_value = {
            "id": "12345",
            "title": "Test Page",
            "body": {"storage": {"value": "<p>Hello</p>"}},
            "space": {"key": "ENG"},
        }

        page = confluence_client.get_page("12345")
        assert page is not None
        assert page["title"] == "Test Page"
        confluence_client._client.get_page_by_id.assert_called_once_with(
            "12345", expand="body.storage,space,version",
        )

    def test_get_page_not_found(self, confluence_client):
        confluence_client._client.get_page_by_id.side_effect = Exception("Not found")
        page = confluence_client.get_page("99999")
        assert page is None

    def test_get_page_unconfigured(self, unconfigured_client):
        page = unconfigured_client.get_page("12345")
        assert page is None


# Tests — get_page_content_as_text

class TestGetPageContentAsText:
    def test_extract_text_from_html(self, confluence_client):
        confluence_client._client.get_page_by_id.return_value = {
            "id": "1",
            "title": "Page",
            "body": {
                "storage": {
                    "value": "<h1>Title</h1><p>Hello <b>world</b>.</p><p>Second paragraph.</p>",
                }
            },
        }

        text = confluence_client.get_page_content_as_text("1")
        assert text is not None
        assert "Hello" in text
        assert "world" in text
        assert "Second paragraph" in text
        # HTML tags should be stripped
        assert "<p>" not in text
        assert "<b>" not in text

    def test_empty_body(self, confluence_client):
        confluence_client._client.get_page_by_id.return_value = {
            "id": "1",
            "title": "Page",
            "body": {"storage": {"value": ""}},
        }

        text = confluence_client.get_page_content_as_text("1")
        assert text is None

    def test_page_not_found(self, confluence_client):
        confluence_client._client.get_page_by_id.side_effect = Exception("404")
        text = confluence_client.get_page_content_as_text("999")
        assert text is None


# Tests — search_pages

class TestSearchPages:
    def test_search_by_space(self, confluence_client):
        confluence_client._client.cql.return_value = {
            "results": [
                {"content": {"id": "1", "title": "Page A", "space": {"key": "ENG"}}},
                {"content": {"id": "2", "title": "Page B", "space": {"key": "ENG"}}},
            ]
        }

        pages = confluence_client.search_pages(space_key="ENG")
        assert len(pages) == 2
        assert pages[0]["id"] == "1"
        assert pages[0]["title"] == "Page A"

    def test_search_by_label(self, confluence_client):
        confluence_client._client.cql.return_value = {"results": []}
        confluence_client.search_pages(space_key="ENG", label="troubleshooting")
        call_args = confluence_client._client.cql.call_args
        cql = call_args[0][0]
        assert 'label="troubleshooting"' in cql

    def test_search_unconfigured(self, unconfigured_client):
        pages = unconfigured_client.search_pages(space_key="ENG")
        assert pages == []

    def test_search_failure(self, confluence_client):
        confluence_client._client.cql.side_effect = Exception("API error")
        pages = confluence_client.search_pages(space_key="ENG")
        assert pages == []


# Tests — _html_to_text

class TestHtmlToText:
    def test_strip_tags(self):
        assert "Hello" in ConfluenceClient._html_to_text("<p>Hello</p>")

    def test_decode_entities(self):
        text = ConfluenceClient._html_to_text("&amp; &lt; &gt;")
        assert "&" in text
        assert "<" in text
        assert ">" in text

    def test_block_tags_become_newlines(self):
        text = ConfluenceClient._html_to_text("<h1>Title</h1><p>Body</p>")
        assert "\n" in text

    def test_script_removal(self):
        text = ConfluenceClient._html_to_text(
            "<p>Before</p><script>alert('x')</script><p>After</p>"
        )
        assert "alert" not in text
        assert "Before" in text
        assert "After" in text

    def test_collapse_whitespace(self):
        text = ConfluenceClient._html_to_text("<p>  lots   of    spaces  </p>")
        assert "  " not in text  # no double spaces
