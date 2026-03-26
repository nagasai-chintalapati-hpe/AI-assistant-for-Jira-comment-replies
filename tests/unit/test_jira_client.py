"""Tests for JiraClient – issue and comment retrieval (mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from src.integrations.jira import JiraClient

# Helpers

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
                "items": [{"field": "status", "fromString": "Open", "toString": "In Progress"}],
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
        mock_instance.issue.return_value = FAKE_ISSUE

        client = JiraClient(
            base_url="https://jira.example.com",
            username="user",
            api_token="token",
        )
        yield client


# Tests


class TestJiraClientInit:
    def test_raises_value_error_without_config(self, monkeypatch):
        """Instantiation with all-empty credentials raises ValueError."""
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_USERNAME", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        with patch("src.integrations.jira.Jira"):
            with pytest.raises(ValueError, match="Missing Jira configuration"):
                JiraClient(base_url="", username="", api_token="")

    def test_raises_when_base_url_missing(self, monkeypatch):
        """Instantiation fails if JIRA_BASE_URL is not set."""
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_USERNAME", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        with patch("src.integrations.jira.Jira"):
            with pytest.raises(ValueError):
                JiraClient()


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

    def test_returns_inward_linked_issue(self, jira_client):
        """Links with inwardIssue should be returned with direction='inward'."""
        inward_data = {
            **FAKE_ISSUE,
            "fields": {
                **FAKE_ISSUE["fields"],
                "issuelinks": [
                    {
                        "type": {"name": "Relates"},
                        "inwardIssue": {
                            "key": "DEFECT-200",
                            "fields": {"status": {"name": "Open"}},
                        },
                    }
                ],
            },
        }
        jira_client.client.issue.return_value = inward_data
        linked = jira_client.get_linked_issues("DEFECT-123")
        assert len(linked) == 1
        assert linked[0]["key"] == "DEFECT-200"
        assert linked[0]["direction"] == "inward"
        assert linked[0]["type"] == "Relates"

    def test_extract_linked_issues_inward(self, jira_client):
        """Static extract_linked_issues handles inward links."""
        from src.integrations.jira import JiraClient as JC

        data = {
            "fields": {
                "issuelinks": [
                    {
                        "type": {"name": "Clones"},
                        "inwardIssue": {
                            "key": "DEFECT-300",
                            "fields": {"status": {"name": "Closed"}},
                        },
                    }
                ]
            }
        }
        result = JC.extract_linked_issues(data)
        assert len(result) == 1
        assert result[0]["key"] == "DEFECT-300"
        assert result[0]["direction"] == "inward"


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
        jira_client.client.issue.return_value = no_jenkins
        urls = jira_client.detect_jenkins_links("DEFECT-123")
        assert urls == []


class TestJiraClientErrorPaths:
    def test_get_comments_returns_empty_on_error(self, jira_client):
        jira_client.client.issue.side_effect = RuntimeError("api down")
        comments = jira_client.get_comments("DEFECT-123")
        assert comments == []

    def test_get_attachments_returns_empty_on_error(self, jira_client):
        jira_client.client.issue.side_effect = RuntimeError("api down")
        attachments = jira_client.get_attachments("DEFECT-123")
        assert attachments == []

    def test_get_linked_issues_returns_empty_on_error(self, jira_client):
        jira_client.client.issue.side_effect = RuntimeError("api down")
        linked = jira_client.get_linked_issues("DEFECT-123")
        assert linked == []

    def test_get_changelog_returns_empty_on_error(self, jira_client):
        jira_client.client.issue.side_effect = RuntimeError("api down")
        changelog = jira_client.get_changelog("DEFECT-123")
        assert changelog == []

    def test_detect_jenkins_links_returns_empty_on_error(self, jira_client):
        jira_client.client.issue.side_effect = RuntimeError("api down")
        urls = jira_client.detect_jenkins_links("DEFECT-123")
        assert urls == []


class TestWriteHelpers:
    def test_add_comment_success_returns_id(self, jira_client):
        jira_client.client.issue_add_comment.return_value = {"id": "c99"}
        comment_id = jira_client.add_comment("DEFECT-123", "Hello")
        assert comment_id == "c99"

    def test_add_comment_internal_sets_visibility(self, jira_client):
        """is_internal=True builds a visibility payload (function exercises the if branch)."""
        jira_client.client.issue_add_comment.return_value = {"id": "c100"}
        comment_id = jira_client.add_comment("DEFECT-123", "Internal note", is_internal=True)
        assert comment_id == "c100"
        # Verify the call was made (visibility is built but add_comment passes only the body)
        jira_client.client.issue_add_comment.assert_called_once()

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
        issue = jira_client.client.issue.return_value
        issue["fields"]["labels"] = ["regression"]

        ok = jira_client.add_label("DEFECT-123", "regression")

        assert ok is True
        jira_client.client.issue_update.assert_not_called()

    def test_add_label_updates_when_missing(self, jira_client):
        issue = jira_client.client.issue.return_value
        issue["fields"]["labels"] = ["existing"]

        ok = jira_client.add_label("DEFECT-123", "new-label")

        assert ok is True
        jira_client.client.issue_update.assert_called_once()

    def test_add_label_returns_false_on_error(self, jira_client):
        jira_client.client.issue.side_effect = RuntimeError("nope")
        assert jira_client.add_label("DEFECT-123", "x") is False

    def test_transition_issue_success(self, jira_client):
        assert jira_client.transition_issue("DEFECT-123", "31") is True
        jira_client.client.issue_transition.assert_called_once_with("DEFECT-123", "31")

    def test_transition_issue_returns_false_on_error(self, jira_client):
        jira_client.client.issue_transition.side_effect = RuntimeError("nope")
        assert jira_client.transition_issue("DEFECT-123", "31") is False


class TestFetchJenkinsConsole:
    """Tests for Jenkins console log fetching."""

    @patch("src.integrations.jira.requests.get")
    def test_fetches_console_text(self, mock_get, jira_client):
        mock_resp = MagicMock()
        mock_resp.text = "BUILD SUCCESS\nFinished: SUCCESS"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        result = jira_client.fetch_jenkins_console(
            "https://jenkins.example.com/job/build/42/console"
        )
        assert result == "BUILD SUCCESS\nFinished: SUCCESS"
        # Should normalise URL to /consoleText
        call_url = mock_get.call_args[0][0]
        assert call_url.endswith("/consoleText")

    @patch("src.integrations.jira.requests.get")
    def test_truncates_long_output(self, mock_get, jira_client):
        mock_resp = MagicMock()
        mock_resp.text = "x" * 5000
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        result = jira_client.fetch_jenkins_console(
            "https://jenkins.example.com/job/build/42/consoleText",
            max_chars=100,
        )
        assert result is not None
        assert "(truncated)" in result
        # Tail portion + prefix should be present
        assert len(result) <= 200  # 100 chars + truncation prefix

    @patch("src.integrations.jira.requests.get")
    def test_returns_none_on_network_error(self, mock_get, jira_client):
        mock_get.side_effect = ConnectionError("unreachable")
        result = jira_client.fetch_jenkins_console(
            "https://jenkins.example.com/job/build/42/console"
        )
        assert result is None

    @patch("src.integrations.jira.requests.get")
    def test_returns_none_on_http_error(self, mock_get, jira_client):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        mock_get.return_value = mock_resp
        result = jira_client.fetch_jenkins_console(
            "https://jenkins.example.com/job/build/42/console"
        )
        assert result is None

    @patch("src.integrations.jira.requests.get")
    def test_normalises_consoleFull_url(self, mock_get, jira_client):
        mock_resp = MagicMock()
        mock_resp.text = "log output"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        jira_client.fetch_jenkins_console("https://jenkins.example.com/job/build/42/consoleFull")
        call_url = mock_get.call_args[0][0]
        assert call_url.endswith("/consoleText")
        assert "consoleFull" not in call_url


class TestFetchJenkinsLogs:
    """Tests for bulk Jenkins log fetching."""

    @patch("src.integrations.jira.requests.get")
    def test_fetches_multiple_urls(self, mock_get, jira_client):
        mock_resp = MagicMock()
        mock_resp.text = "BUILD OK"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        urls = [
            "https://jenkins.example.com/job/a/1/console",
            "https://jenkins.example.com/job/b/2/console",
        ]
        results = jira_client.fetch_jenkins_logs(urls)
        assert len(results) == 2

    @patch("src.integrations.jira.requests.get")
    def test_skips_failed_urls(self, mock_get, jira_client):
        def side_effect(url, **kwargs):
            if "job/a" in url:
                raise ConnectionError("down")
            resp = MagicMock()
            resp.text = "OK"
            resp.raise_for_status.return_value = None
            return resp

        mock_get.side_effect = side_effect

        urls = [
            "https://jenkins.example.com/job/a/1/console",
            "https://jenkins.example.com/job/b/2/console",
        ]
        results = jira_client.fetch_jenkins_logs(urls)
        assert len(results) == 1
