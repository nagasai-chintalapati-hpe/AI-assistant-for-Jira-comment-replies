"""Tests for ContextCollector – uses mocked JiraClient."""

from unittest.mock import MagicMock

import pytest

from src.agent.context_collector import ContextCollector

# Helpers


def _fake_issue_data() -> dict:
    """Compact Jira issue JSON fixture for collector tests."""
    return {
        "key": "TEST-200",
        "fields": {
            "summary": "Sample issue summary",
            "description": (
                "Failure observed in staging. "
                "See https://jenkins.example.com/job/build/42/consoleFull for logs."
            ),
            "issuetype": {"name": "Bug"},
            "status": {"name": "In Progress"},
            "priority": {"name": "High"},
            "environment": "staging",
            "versions": [{"name": "1.8.14"}],
            "fixVersions": [{"name": "1.8.15"}],
            "components": [{"name": "Service A"}],
            "labels": ["test"],
            "comment": {
                "comments": [
                    {
                        "id": "c1",
                        "author": {"displayName": "Alice"},
                        "created": "2025-02-20T09:00:00Z",
                        "body": "Initial report",
                    },
                    {
                        "id": "c2",
                        "author": {"displayName": "Bob"},
                        "created": "2025-02-21T10:00:00Z",
                        "body": "Cannot reproduce",
                    },
                    {
                        "id": "c3",
                        "author": {"displayName": "Alice"},
                        "created": "2025-02-22T11:00:00Z",
                        "body": "Added logs",
                    },
                ]
            },
            "attachment": [
                {
                    "id": "a1",
                    "filename": "artifact.log",
                    "content": "https://jira.example.com/att/1",
                    "mimeType": "text/plain",
                    "size": 4096,
                    "created": "2025-02-22T11:01:00Z",
                },
            ],
            "issuelinks": [
                {
                    "type": {"name": "Blocks"},
                    "inwardIssue": {
                        "key": "TEST-100",
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
    """Return a MagicMock that mimics JiraClient with sensible defaults.

    Uses the real static extract methods on _fake_issue_data() so the
    fixture data is defined in exactly one place.
    """
    from src.integrations.jira import JiraClient

    client = MagicMock()
    data = _fake_issue_data()

    client.get_issue.return_value = data

    # Delegate to real static extractors — no duplication of fixture values
    client.extract_attachments.side_effect = lambda d: JiraClient.extract_attachments(d)
    client.extract_linked_issues.side_effect = lambda d: JiraClient.extract_linked_issues(d)
    client.extract_changelog.side_effect = lambda d: JiraClient.extract_changelog(d)
    client.extract_jenkins_links.side_effect = lambda d: JiraClient.extract_jenkins_links(d)

    # Jenkins log fetch is an HTTP call, so we still mock its return value
    client.fetch_jenkins_logs.return_value = {
        "https://jenkins.example.com/job/build/42/consoleFull": (
            "BUILD FAILURE\njava.lang.NullPointerException\n"
            "  at com.app.Snapshot.lock(Snapshot.java:42)"
        )
    }
    return client


@pytest.fixture
def collector(mock_jira):
    c = ContextCollector(jira_client=mock_jira)
    return c


# Tests


class TestContextCollector:
    def test_collect_returns_result(self, collector):
        result = collector.collect("TEST-200")
        assert result.issue_context.issue_key == "TEST-200"
        assert result.collection_duration_ms >= 0

    def test_issue_fields_populated(self, collector):
        ctx = collector.collect("TEST-200").issue_context
        assert ctx.summary == "Sample issue summary"
        assert ctx.status == "In Progress"
        assert ctx.priority == "High"
        assert ctx.environment == "staging"

    def test_versions_deduplicated(self, collector):
        ctx = collector.collect("TEST-200").issue_context
        assert "1.8.14" in ctx.versions
        assert "1.8.15" in ctx.versions

    def test_last_comments_populated(self, collector):
        ctx = collector.collect("TEST-200").issue_context
        assert ctx.last_comments is not None
        assert len(ctx.last_comments) == 3  # all 3 comments (max_comments default=10)

    def test_attachments_populated(self, collector):
        ctx = collector.collect("TEST-200").issue_context
        assert len(ctx.attached_files) == 1
        assert ctx.attached_files[0]["filename"] == "artifact.log"

    def test_linked_issues_populated(self, collector):
        ctx = collector.collect("TEST-200").issue_context
        assert len(ctx.linked_issues) == 1
        assert ctx.linked_issues[0]["key"] == "TEST-100"

    def test_changelog_populated(self, collector):
        ctx = collector.collect("TEST-200").issue_context
        assert ctx.changelog is not None
        assert len(ctx.changelog) == 1

    def test_jenkins_links_populated(self, collector):
        result = collector.collect("TEST-200")
        assert result.jenkins_links is not None
        assert "jenkins" in result.jenkins_links[0].lower()

    def test_jenkins_log_snippets_populated(self, collector):
        result = collector.collect("TEST-200")
        assert result.jenkins_log_snippets is not None
        assert len(result.jenkins_log_snippets) == 1
        snippet = list(result.jenkins_log_snippets.values())[0]
        assert "BUILD FAILURE" in snippet

    def test_collect_with_custom_max_comments(self, collector, mock_jira):
        """max_comments limits how many comments are included from the pre-fetched data."""
        result = collector.collect("TEST-200", max_comments=1)
        # Only the last 1 of 3 comments should be included
        assert len(result.issue_context.last_comments) == 1

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
        client.extract_attachments.return_value = []
        client.extract_linked_issues.return_value = []
        client.extract_changelog.return_value = []
        client.extract_jenkins_links.return_value = []
        client.fetch_jenkins_logs.return_value = {}

        c = ContextCollector(jira_client=client)
        result = c.collect("DEFECT-999")
        assert result.issue_context.summary == "Minimal"
        assert result.issue_context.last_comments == []
