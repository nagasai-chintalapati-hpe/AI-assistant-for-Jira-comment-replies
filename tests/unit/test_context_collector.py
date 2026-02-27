"""Tests for ContextCollector – uses mocked JiraClient."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from src.agent.context_collector import ContextCollector


# ---- helpers ----------------------------------------------------------- #

def _fake_issue_data() -> dict:
    """Minimal Jira issue JSON that mirrors the real API shape."""
    return {
        "key": "DEFECT-200",
        "fields": {
            "summary": "Upload fails with 500 error",
            "description": "POST /snapshot returns SnapshotLockTimeout",
            "issuetype": {"name": "Bug"},
            "status": {"name": "In Progress"},
            "priority": {"name": "High"},
            "environment": "Staging, Chrome 121, tenant X",
            "versions": [{"name": "1.8.14"}],
            "fixVersions": [{"name": "1.8.15"}],
            "components": [{"name": "Snapshot Service"}],
            "labels": ["upload", "p1"],
            "comment": {
                "comments": [
                    {"id": "c1", "author": {"displayName": "Alice"}, "created": "2025-02-20T09:00:00Z", "body": "Opened ticket"},
                    {"id": "c2", "author": {"displayName": "Bob"}, "created": "2025-02-21T10:00:00Z", "body": "Cannot reproduce"},
                    {"id": "c3", "author": {"displayName": "Alice"}, "created": "2025-02-22T11:00:00Z", "body": "Added logs"},
                ]
            },
            "attachment": [
                {"id": "a1", "filename": "error.log", "content": "https://jira/att/1", "mimeType": "text/plain", "size": 4096, "created": "2025-02-22T11:01:00Z"},
            ],
            "issuelinks": [
                {
                    "type": {"name": "Blocks"},
                    "inwardIssue": {
                        "key": "DEFECT-100",
                        "fields": {"status": {"name": "Closed"}},
                    },
                }
            ],
        },
        "changelog": {
            "histories": [
                {
                    "author": {"displayName": "CI Bot"},
                    "created": "2025-02-20T08:00:00Z",
                    "items": [{"field": "status", "fromString": "Open", "toString": "In Progress"}],
                }
            ]
        },
    }


@pytest.fixture
def mock_jira():
    """Return a MagicMock that mimics JiraClient with sensible defaults."""
    client = MagicMock()
    data = _fake_issue_data()

    client.get_issue.return_value = data
    client.get_last_comments.return_value = data["fields"]["comment"]["comments"][-2:]
    client.get_attachments.return_value = [
        {"id": "a1", "filename": "error.log", "content_url": "https://jira/att/1",
         "mime_type": "text/plain", "size": 4096, "created": "2025-02-22T11:01:00Z"},
    ]
    client.get_linked_issues.return_value = [
        {"key": "DEFECT-100", "type": "Blocks", "direction": "inward", "status": "Closed"},
    ]
    client.get_changelog.return_value = [
        {"author": "CI Bot", "created": "2025-02-20T08:00:00Z",
         "items": [{"field": "status", "from": "Open", "to": "In Progress"}]},
    ]
    client.detect_jenkins_links.return_value = [
        "https://jenkins.company.com/job/snapshot/42/consoleFull"
    ]
    return client


@pytest.fixture
def collector(mock_jira):
    c = ContextCollector(jira_client=mock_jira)
    return c


# ---- tests ------------------------------------------------------------- #

class TestContextCollector:
    def test_collect_returns_result(self, collector):
        result = collector.collect("DEFECT-200")
        assert result.issue_context.issue_key == "DEFECT-200"
        assert result.collection_duration_ms >= 0

    def test_issue_fields_populated(self, collector):
        ctx = collector.collect("DEFECT-200").issue_context
        assert ctx.summary == "Upload fails with 500 error"
        assert ctx.status == "In Progress"
        assert ctx.priority == "High"
        assert ctx.environment == "Staging, Chrome 121, tenant X"

    def test_versions_deduplicated(self, collector):
        ctx = collector.collect("DEFECT-200").issue_context
        assert "1.8.14" in ctx.versions
        assert "1.8.15" in ctx.versions

    def test_last_comments_populated(self, collector):
        ctx = collector.collect("DEFECT-200").issue_context
        assert ctx.last_comments is not None
        assert len(ctx.last_comments) == 2  # last 2 of 3

    def test_attachments_populated(self, collector):
        ctx = collector.collect("DEFECT-200").issue_context
        assert len(ctx.attached_files) == 1
        assert ctx.attached_files[0]["filename"] == "error.log"

    def test_linked_issues_populated(self, collector):
        ctx = collector.collect("DEFECT-200").issue_context
        assert len(ctx.linked_issues) == 1
        assert ctx.linked_issues[0]["key"] == "DEFECT-100"

    def test_changelog_populated(self, collector):
        ctx = collector.collect("DEFECT-200").issue_context
        assert ctx.changelog is not None
        assert len(ctx.changelog) == 1

    def test_jenkins_links_populated(self, collector):
        result = collector.collect("DEFECT-200")
        assert result.jenkins_links is not None
        assert "jenkins" in result.jenkins_links[0].lower()

    def test_collect_with_custom_max_comments(self, collector, mock_jira):
        """max_comments is forwarded to JiraClient."""
        collector.collect("DEFECT-200", max_comments=5)
        mock_jira.get_last_comments.assert_called_once_with("DEFECT-200", n=5)

    def test_collect_graceful_on_empty_fields(self):
        """Should not crash when Jira returns sparse data."""
        client = MagicMock()
        client.get_issue.return_value = {
            "key": "DEFECT-999",
            "fields": {
                "summary": "Minimal",
                "description": None,
                "issuetype": {"name": "Bug"},
                "status": {"name": "Open"},
                "priority": {"name": "Low"},
            },
        }
        client.get_last_comments.return_value = []
        client.get_attachments.return_value = []
        client.get_linked_issues.return_value = []
        client.get_changelog.return_value = []
        client.detect_jenkins_links.return_value = []

        c = ContextCollector(jira_client=client)
        result = c.collect("DEFECT-999")
        assert result.issue_context.summary == "Minimal"
        assert result.issue_context.last_comments == []
