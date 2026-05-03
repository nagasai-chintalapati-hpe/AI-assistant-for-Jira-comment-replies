"""Tests for webhook event filtering and validation"""

import pytest
from src.models.webhook import JiraWebhookEvent
from src.api.event_filter import (
    EventFilter,
    ALLOWED_ISSUE_TYPES,
    ALLOWED_STATUSES,
)


@pytest.fixture(autouse=True)
def _default_qa_config(monkeypatch):
    """Ensure a default QA config so Bug tests pass the QA gate."""
    monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
    monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "qa-default-001")
    monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "")
    monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
    import src.config as cfg
    import os
    cfg.settings = cfg.Settings(
        qa_team=cfg.QATeamConfig(
            enabled=True,
            account_ids="qa-default-001",
            display_names="",
            jira_groups="",
        ),
    )
    yield


@pytest.fixture
def event_filter():
    f = EventFilter()
    yield f
    f.reset()


def _make_payload(
    event_type: str = "comment_created",
    issue_type: str = "Bug",
    status: str = "Open",
    comment_body: str = "Cannot reproduce this on my machine.",
    issue_key: str = "DEFECT-100",
    comment_id: str = "20001",
    timestamp: int = 1700000000,
    reporter_account_id: str = "qa-default-001",
    reporter_display_name: str = "QA Default User",
) -> dict:
    """Helper to build a minimal Jira webhook payload dict."""
    return {
        "webhookEvent": event_type,
        "timestamp": timestamp,
        "issue": {
            "id": "1",
            "key": issue_key,
            "fields": {
                "summary": "Test issue",
                "issuetype": {"name": issue_type},
                "status": {"name": status},
                "reporter": {
                    "accountId": reporter_account_id,
                    "displayName": reporter_display_name,
                    "emailAddress": "qa@company.com",
                },
            },
        },
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "author": {
                "accountId": "abc123",
                "displayName": "Dev User",
                "emailAddress": "dev@company.com",
            },
            "created": "2025-02-23T10:30:00.000+0000",
            "updated": "2025-02-23T10:30:00.000+0000",
        },
    }


# ---- Parsing ------------------------------------------------------------ #

class TestWebhookEventParsing:
    def test_parse_valid_payload(self):
        payload = _make_payload()
        event = JiraWebhookEvent(**payload)
        assert event.issue_key == "DEFECT-100"
        assert event.comment is not None
        assert event.comment.id == "20001"

    def test_event_id_deterministic(self):
        payload = _make_payload()
        e1 = JiraWebhookEvent(**payload)
        e2 = JiraWebhookEvent(**payload)
        assert e1.event_id == e2.event_id

    def test_parse_minimal_payload(self):
        """Payload with only webhookEvent should parse (issue/comment optional)."""
        event = JiraWebhookEvent(webhookEvent="jira:issue_updated")
        assert event.issue is None
        assert event.comment is None


# ---- Filtering ---------------------------------------------------------- #

class TestEventFilter:
    def test_accept_valid_bug_comment(self, event_filter):
        payload = _make_payload()
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_reject_non_bug_issue_type(self, event_filter):
        payload = _make_payload(issue_type="Custom Unknown Type")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "not in the allowed set" in result.reason or "not Bug/Defect" in result.reason

    def test_reject_disallowed_status(self, event_filter):
        payload = _make_payload(status="Archived")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "not in the allowed set" in result.reason

    def test_reject_unhandled_event_type(self, event_filter):
        payload = _make_payload(event_type="issue_deleted")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "Unhandled event" in result.reason

    def test_reject_no_issue(self, event_filter):
        event = JiraWebhookEvent(webhookEvent="comment_created")
        result = event_filter.evaluate(event)
        assert result.accepted is False

    def test_accept_comment_without_keywords(self, event_filter):
        """All comments on Bug/Defect issues are accepted — keyword matching
        is informational only (used by classifier, not as a gate)."""
        payload = _make_payload(comment_body="Looks good to me!")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_idempotency_rejects_duplicate(self, event_filter):
        payload = _make_payload()
        event = JiraWebhookEvent(**payload)
        first = event_filter.evaluate(event)
        assert first.accepted is True

        second = event_filter.evaluate(event)
        assert second.accepted is False
        assert "Duplicate" in second.reason

    def test_different_keywords_accepted(self, event_filter):
        """All trigger keywords should pass the filter."""
        keywords = [
            "need logs please",
            "This is as designed",
            "fix ready in v2.3",
        ]
        for i, kw in enumerate(keywords):
            event_filter.reset()
            payload = _make_payload(comment_body=kw, comment_id=str(30000 + i))
            event = JiraWebhookEvent(**payload)
            result = event_filter.evaluate(event)
            assert result.accepted is True, f"Keyword '{kw}' should pass filter"


# ---- QA Team Filtering -------------------------------------------------- #


def _make_payload_with_reporter(
    reporter_account_id: str = "qa-user-001",
    reporter_display_name: str = "QA Engineer",
    reporter_email: str = "qa@company.com",
    issue_type: str = "Bug",
    **kwargs,
) -> dict:
    """Build a webhook payload that includes a reporter field."""
    base = _make_payload(issue_type=issue_type, **kwargs)
    base["issue"]["fields"]["reporter"] = {
        "accountId": reporter_account_id,
        "displayName": reporter_display_name,
        "emailAddress": reporter_email,
    }
    return base


class TestQATeamFilter:
    """Tests for QA team gating on Bug/Defect issues."""

    def test_bug_from_qa_member_accepted(self, event_filter, monkeypatch):
        """Bug raised by a recognised QA accountId passes the filter."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "qa-user-001,qa-user-002")
        monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "")
        monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
        self._reload_settings()

        payload = _make_payload_with_reporter(reporter_account_id="qa-user-001")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_bug_from_non_qa_rejected(self, event_filter, monkeypatch):
        """Bug raised by a non-QA reporter is rejected."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "qa-user-001")
        monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "")
        monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
        self._reload_settings()

        payload = _make_payload_with_reporter(reporter_account_id="dev-user-999")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "not a recognised QA team member" in result.reason

    def test_display_name_fallback(self, event_filter, monkeypatch):
        """QA member matched by display-name substring passes."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "")
        monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "qa engineer,tester")
        monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
        self._reload_settings()

        payload = _make_payload_with_reporter(
            reporter_account_id="unknown-id",
            reporter_display_name="Senior QA Engineer",
        )
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_non_defect_issue_bypasses_qa_gate(self, event_filter, monkeypatch):
        """Story/Task issues are NOT gated by QA filter."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "qa-user-001")
        monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "")
        monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
        self._reload_settings()

        payload = _make_payload_with_reporter(
            reporter_account_id="dev-user-999",
            issue_type="Story",
        )
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_qa_filter_disabled_allows_all(self, event_filter, monkeypatch):
        """When QA_TEAM_FILTER_ENABLED=false, all reporters pass."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "false")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "qa-user-001")
        self._reload_settings()

        payload = _make_payload_with_reporter(reporter_account_id="anyone")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_no_identifiers_configured_allows_all(self, event_filter, monkeypatch):
        """When filter is enabled but no IDs/names configured, allow all (safe default)."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "")
        monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "")
        monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
        self._reload_settings()

        payload = _make_payload_with_reporter(reporter_account_id="anyone")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_defect_type_also_gated(self, event_filter, monkeypatch):
        """Issue type 'Defect' (not just 'Bug') is also QA-gated."""
        monkeypatch.setenv("QA_TEAM_FILTER_ENABLED", "true")
        monkeypatch.setenv("QA_TEAM_ACCOUNT_IDS", "qa-user-001")
        monkeypatch.setenv("QA_TEAM_DISPLAY_NAMES", "")
        monkeypatch.setenv("QA_TEAM_JIRA_GROUPS", "")
        self._reload_settings()

        payload = _make_payload_with_reporter(
            reporter_account_id="dev-user-999",
            issue_type="Defect",
        )
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False

    @staticmethod
    def _reload_settings():
        """Force re-create the QATeamConfig from current env vars."""
        import src.config as cfg
        import os

        cfg.settings = cfg.Settings(
            qa_team=cfg.QATeamConfig(
                enabled=os.getenv("QA_TEAM_FILTER_ENABLED", "true").lower() == "true",
                account_ids=os.getenv("QA_TEAM_ACCOUNT_IDS", ""),
                display_names=os.getenv("QA_TEAM_DISPLAY_NAMES", ""),
                jira_groups=os.getenv("QA_TEAM_JIRA_GROUPS", ""),
            ),
        )
