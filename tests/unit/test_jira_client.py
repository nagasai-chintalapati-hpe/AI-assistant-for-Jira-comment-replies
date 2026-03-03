"""Tests for JiraClient – full issue and comment retrieval (MVP v1).

All tests mock the underlying atlassian.Jira client so we never
hit a real Jira instance.
"""

import pytest
from unittest.mock import MagicMock, patch
from src.integrations.jira import JiraClient


# ---- Helpers ------------------------------------------------------------ #

FAKE_ISSUE = {
    "key": "DEFECT-123",
    "fields": {
        "summary": "UI crash on upload",
        "description": "Steps to repro: ... https://jenkins.example.com/job/build/123/console ...",
        "issuetype": {"name": "Bug"},
        "status": {"name": "Open"},
        "priority": {"name": "High"},
        "environment": "Chrome 120 / macOS",
        "labels": ["regression"],
        "versions": [{"name": "1.8.14"}],
        "fixVersions": [{"name": "1.8.15"}],
        "components": [{"name": "Upload Service"}],
        "attachment": [
            {
                "id": "att-1",
                "filename": "screenshot.png",
                "content": "https://jira.example.com/att/1",
                "mimeType": "image/png",
                "size": 204800,
                "created": "2025-02-20T08:00:00Z",
            }
        ],
        "issuelinks": [
            {
                "type": {"name": "Blocks"},
                "outwardIssue": {
                    "key": "DEFECT-100",
                    "fields": {"status": {"name": "Closed"}},
                },
            }
        ],
        "comment": {
            "comments": [
                {"id": "c1", "body": "First comment", "author": {"displayName": "Alice"}},
                {"id": "c2", "body": "Second comment", "author": {"displayName": "Bob"}},
                {"id": "c3", "body": "Third comment", "author": {"displayName": "Carol"}},
            ]
        },
    },
    "changelog": {
        "histories": [
            {
                "author": {"displayName": "Alice"},
                "created": "2025-02-21T09:00:00Z",
                "items": [
                    {"field": "status", "fromString": "Open", "toString": "In Progress"}
                ],
            }
        ]
    },
}


@pytest.fixture
def jira_client():
    """Create a JiraClient with a mocked underlying atlassian.Jira."""
    with patch("src.integrations.jira.Jira") as MockJira:
        mock_instance = MagicMock()
        MockJira.return_value = mock_instance
        mock_instance.issue_get.return_value = FAKE_ISSUE

        client = JiraClient(
            base_url="https://jira.example.com",
            username="user",
            api_token="token",
        )
        yield client


# ---- Tests -------------------------------------------------------------- #


class TestGetIssue:
    def test_returns_issue_dict(self, jira_client):
        issue = jira_client.get_issue("DEFECT-123")
        assert issue["key"] == "DEFECT-123"
        assert issue["fields"]["summary"] == "UI crash on upload"


class TestGetComments:
    def test_returns_all_comments(self, jira_client):
        comments = jira_client.get_comments("DEFECT-123")
        assert len(comments) == 3

    def test_get_last_comments_default(self, jira_client):
        """Default n=10 should return all 3 comments."""
        comments = jira_client.get_last_comments("DEFECT-123")
        assert len(comments) == 3

    def test_get_last_comments_limited(self, jira_client):
        comments = jira_client.get_last_comments("DEFECT-123", n=2)
        assert len(comments) == 2
        assert comments[0]["id"] == "c2"
        assert comments[1]["id"] == "c3"


class TestGetAttachments:
    def test_returns_attachment_metadata(self, jira_client):
        attachments = jira_client.get_attachments("DEFECT-123")
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "screenshot.png"
        assert attachments[0]["mime_type"] == "image/png"


class TestGetLinkedIssues:
    def test_returns_linked_issues(self, jira_client):
        linked = jira_client.get_linked_issues("DEFECT-123")
        assert len(linked) == 1
        assert linked[0]["key"] == "DEFECT-100"
        assert linked[0]["type"] == "Blocks"
        assert linked[0]["direction"] == "outward"


class TestGetChangelog:
    def test_returns_changelog_entries(self, jira_client):
        log = jira_client.get_changelog("DEFECT-123")
        assert len(log) == 1
        assert log[0]["author"] == "Alice"
        assert log[0]["items"][0]["field"] == "status"
        assert log[0]["items"][0]["to"] == "In Progress"


class TestDetectJenkinsLinks:
    def test_finds_jenkins_url_in_description(self, jira_client):
        urls = jira_client.detect_jenkins_links("DEFECT-123")
        assert len(urls) >= 1
        assert "jenkins.example.com" in urls[0]

    def test_no_jenkins_links(self, jira_client):
        """Issue with no Jenkins URLs should return empty list."""
        no_jenkins = {
            **FAKE_ISSUE,
            "fields": {
                **FAKE_ISSUE["fields"],
                "description": "No jenkins here",
                "comment": {"comments": []},
            },
        }
        jira_client.client.issue_get.return_value = no_jenkins
        urls = jira_client.detect_jenkins_links("DEFECT-123")
        assert urls == []


class TestJiraClientErrorPaths:
    def test_get_comments_returns_empty_on_error(self, jira_client):
        jira_client.client.issue_get.side_effect = RuntimeError("api down")
        comments = jira_client.get_comments("DEFECT-123")
        assert comments == []

    def test_get_attachments_returns_empty_on_error(self, jira_client):
        jira_client.client.issue_get.side_effect = RuntimeError("api down")
        attachments = jira_client.get_attachments("DEFECT-123")
        assert attachments == []

    def test_get_linked_issues_returns_empty_on_error(self, jira_client):
        jira_client.client.issue_get.side_effect = RuntimeError("api down")
        linked = jira_client.get_linked_issues("DEFECT-123")
        assert linked == []

    def test_get_changelog_returns_empty_on_error(self, jira_client):
        jira_client.client.issue_get.side_effect = RuntimeError("api down")
        changelog = jira_client.get_changelog("DEFECT-123")
        assert changelog == []

    def test_detect_jenkins_links_returns_empty_on_error(self, jira_client):
        jira_client.client.issue_get.side_effect = RuntimeError("api down")
        urls = jira_client.detect_jenkins_links("DEFECT-123")
        assert urls == []


class TestWriteHelpers:
    def test_add_comment_success_returns_id(self, jira_client):
        jira_client.client.issue_add_comment.return_value = {"id": "c99"}
        comment_id = jira_client.add_comment("DEFECT-123", "Hello")
        assert comment_id == "c99"

    def test_add_comment_raises_on_error(self, jira_client):
        jira_client.client.issue_add_comment.side_effect = RuntimeError("nope")
        with pytest.raises(RuntimeError):
            jira_client.add_comment("DEFECT-123", "Hello")

    def test_update_custom_field_success(self, jira_client):
        assert jira_client.update_custom_field("DEFECT-123", "customfield_1", "value") is True

    def test_update_custom_field_returns_false_on_error(self, jira_client):
        jira_client.client.issue_update.side_effect = RuntimeError("nope")
        assert jira_client.update_custom_field("DEFECT-123", "customfield_1", "value") is False

    def test_add_label_does_not_update_if_present(self, jira_client):
        issue = jira_client.client.issue_get.return_value
        issue["fields"]["labels"] = ["regression"]

        ok = jira_client.add_label("DEFECT-123", "regression")

        assert ok is True
        jira_client.client.issue_update.assert_not_called()

    def test_add_label_updates_when_missing(self, jira_client):
        issue = jira_client.client.issue_get.return_value
        issue["fields"]["labels"] = ["existing"]

        ok = jira_client.add_label("DEFECT-123", "new-label")

        assert ok is True
        jira_client.client.issue_update.assert_called_once()

    def test_add_label_returns_false_on_error(self, jira_client):
        jira_client.client.issue_get.side_effect = RuntimeError("nope")
        assert jira_client.add_label("DEFECT-123", "x") is False

    def test_transition_issue_success(self, jira_client):
        assert jira_client.transition_issue("DEFECT-123", "31") is True
        jira_client.client.issue_transition.assert_called_once_with("DEFECT-123", "31")

    def test_transition_issue_returns_false_on_error(self, jira_client):
        jira_client.client.issue_transition.side_effect = RuntimeError("nope")
        assert jira_client.transition_issue("DEFECT-123", "31") is False
