"""Tests for Confluence text search, citations, and PDF extraction."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from src.integrations.confluence import ConfluenceClient, ConfluenceCitation


def _make_client():
    with patch("src.integrations.confluence.settings") as ms:
        ms.confluence.base_url = "https://wiki.example.com"
        ms.confluence.username = "user@co.com"
        ms.confluence.api_token = "token"
        ms.confluence.spaces = "ENG,QA"
        ms.confluence.labels = "runbook"
        with patch("src.integrations.confluence.ConfluenceClient.__init__", return_value=None):
            client = ConfluenceClient.__new__(ConfluenceClient)
            client._base_url = "https://wiki.example.com"
            client._username = "user@co.com"
            client._api_token = "token"
            client._spaces = ["ENG", "QA"]
            client._labels = ["runbook"]
            client._client = MagicMock()
    return client


class TestConfluenceCitation:
    def test_to_citation_dict(self):
        cite = ConfluenceCitation(
            page_id="123",
            title="Deployment Guide",
            space_key="ENG",
            url="https://wiki.example.com/wiki/spaces/ENG/pages/123",
            excerpt="This guide covers deployment steps for v2.0...",
        )
        d = cite.to_citation_dict()
        assert d["source"] == "Confluence: Deployment Guide"
        assert d["url"] == "https://wiki.example.com/wiki/spaces/ENG/pages/123"
        assert "deployment" in d["excerpt"].lower()
        assert d["type"] == "confluence"

    def test_pdf_source_type(self):
        cite = ConfluenceCitation(
            page_id="456",
            title="Test Plan — plan.pdf",
            space_key="QA",
            url="https://wiki.example.com/wiki/spaces/QA/pages/456",
            excerpt="Test plan for regression...",
            source_type="pdf",
        )
        assert cite.to_citation_dict()["type"] == "pdf"


class TestSearchByText:
    def test_returns_citations(self):
        client = _make_client()
        client._client.cql.return_value = {
            "results": [
                {
                    "content": {
                        "id": "100",
                        "title": "Troubleshooting LDAP",
                        "space": {"key": "ENG"},
                        "body": {
                            "storage": {
                                "value": "<p>LDAP timeout causes login failure on v2.3</p>"
                            }
                        },
                    }
                }
            ]
        }
        results = client.search_by_text("LDAP timeout")
        assert len(results) == 1
        assert results[0].title == "Troubleshooting LDAP"
        assert results[0].space_key == "ENG"
        assert "LDAP timeout" in results[0].excerpt

    def test_uses_configured_spaces(self):
        client = _make_client()
        client._client.cql.return_value = {"results": []}
        client.search_by_text("test query")
        cql_arg = client._client.cql.call_args[0][0]
        assert 'space="ENG"' in cql_arg or 'space="QA"' in cql_arg

    def test_empty_when_not_configured(self):
        client = _make_client()
        client._client = None
        results = client.search_by_text("query")
        assert results == []

    def test_space_key_override(self):
        client = _make_client()
        client._client.cql.return_value = {"results": []}
        client.search_by_text("query", space_key="CUSTOM")
        cql_arg = client._client.cql.call_args[0][0]
        assert 'space="CUSTOM"' in cql_arg


class TestGetPageWithCitation:
    def test_returns_citation_object(self):
        client = _make_client()
        client.get_page = MagicMock(return_value={
            "id": "200",
            "title": "Release Notes",
            "space": {"key": "ENG"},
            "body": {
                "storage": {
                    "value": "<p>Release 2.4 includes fix for PROJ-100</p>"
                }
            },
        })
        cite = client.get_page_with_citation("200")
        assert cite is not None
        assert cite.title == "Release Notes"
        assert "Release 2.4" in cite.full_text

    def test_returns_none_when_not_found(self):
        client = _make_client()
        client.get_page = MagicMock(return_value=None)
        assert client.get_page_with_citation("999") is None


class TestFetchPdfAttachment:
    def test_returns_none_when_not_configured(self):
        client = _make_client()
        client._client = None
        assert client.fetch_pdf_attachment("123") is None

    def test_returns_none_when_no_pdf(self):
        client = _make_client()
        client._client.get_attachments_from_content.return_value = {
            "results": [
                {"title": "screenshot.png", "_links": {"download": "/dl/1"}},
            ]
        }
        assert client.fetch_pdf_attachment("123") is None


class TestHtmlToText:
    def test_strips_tags(self):
        result = ConfluenceClient._html_to_text(
            "<p>Hello <strong>world</strong></p>"
        )
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_decodes_entities(self):
        result = ConfluenceClient._html_to_text("&amp; &lt; &gt;")
        assert "& < >" == result

    def test_collapses_whitespace(self):
        result = ConfluenceClient._html_to_text("<p>a</p><p>b</p><p>c</p>")
        assert "\n\n\n" not in result
