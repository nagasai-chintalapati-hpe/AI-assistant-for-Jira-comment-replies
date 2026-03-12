"""Live integration tests for JiraClient against nagasai42006.atlassian.net.

These tests hit the real Jira Cloud REST API and are skipped automatically
when JIRA_API_TOKEN is not set in the environment.

Run:
    pytest tests/integration/test_jira_live.py -v
"""

import os
import pytest
from dotenv import load_dotenv

load_dotenv(".env", override=True)

from src.integrations.jira import JiraClient

HAVE_JIRA_CREDS = bool(
    os.getenv("JIRA_BASE_URL")
    and os.getenv("JIRA_USERNAME")
    and os.getenv("JIRA_API_TOKEN")
)

pytestmark = pytest.mark.skipif(
    not HAVE_JIRA_CREDS,
    reason="Jira credentials not configured in .env",
)


@pytest.fixture(scope="module")
def jira():
    """Module-scoped JiraClient (one connection for all tests)."""
    return JiraClient()


ISSUE_KEY = "IP-7"


class TestJiraConnection:
    """Verify basic connectivity and authentication."""

    def test_get_issue_returns_dict(self, jira):
        issue = jira.get_issue(ISSUE_KEY)
        assert isinstance(issue, dict)
        assert issue.get("key") == ISSUE_KEY

    def test_issue_has_summary(self, jira):
        issue = jira.get_issue(ISSUE_KEY)
        summary = issue["fields"]["summary"]
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_issue_has_status(self, jira):
        issue = jira.get_issue(ISSUE_KEY)
        status = issue["fields"]["status"]["name"]
        assert isinstance(status, str)

    def test_issue_has_issuetype(self, jira):
        issue = jira.get_issue(ISSUE_KEY)
        assert issue["fields"]["issuetype"]["name"] == "Bug"


class TestJiraComments:
    """Verify comment retrieval."""

    def test_get_comments_returns_list(self, jira):
        comments = jira.get_comments(ISSUE_KEY)
        assert isinstance(comments, list)

    def test_get_last_comments(self, jira):
        comments = jira.get_last_comments(ISSUE_KEY, n=5)
        assert isinstance(comments, list)
        assert len(comments) <= 5


class TestJiraAttachments:
    """Verify attachment retrieval."""

    def test_get_attachments_returns_list(self, jira):
        attachments = jira.get_attachments(ISSUE_KEY)
        assert isinstance(attachments, list)
        for att in attachments:
            assert "filename" in att
            assert "mime_type" in att


class TestJiraLinkedIssues:
    """Verify linked-issue retrieval."""

    def test_get_linked_issues_returns_list(self, jira):
        linked = jira.get_linked_issues(ISSUE_KEY)
        assert isinstance(linked, list)
        for link in linked:
            assert "key" in link
            assert "type" in link
            assert "direction" in link


class TestJiraChangelog:
    """Verify changelog retrieval."""

    def test_get_changelog_returns_list(self, jira):
        log = jira.get_changelog(ISSUE_KEY)
        assert isinstance(log, list)


class TestJiraJenkins:
    """Verify Jenkins link detection."""

    def test_detect_jenkins_links_returns_list(self, jira):
        urls = jira.detect_jenkins_links(ISSUE_KEY)
        assert isinstance(urls, list)
