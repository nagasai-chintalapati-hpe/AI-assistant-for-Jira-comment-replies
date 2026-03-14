"""Tests for webhook event filtering and validation"""

import pytest

from src.api.event_filter import (
    EventFilter,
)
from src.models.webhook import JiraWebhookEvent


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


# Parsing


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


# Filtering


class TestEventFilter:
    def test_accept_valid_bug_comment(self, event_filter):
        payload = _make_payload()
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_reject_non_bug_issue_type(self, event_filter):
        payload = _make_payload(issue_type="Story")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "not Bug/Defect" in result.reason

    def test_reject_disallowed_status(self, event_filter):
        payload = _make_payload(status="Done")
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

    def test_accept_any_non_empty_comment(self, event_filter):
        """Any non-empty comment on a Bug should pass the filter — classifier handles buckets."""
        payload = _make_payload(comment_body="Looks good to me!")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is True

    def test_reject_empty_comment_body(self, event_filter):
        """Empty comment body should be filtered out."""
        payload = _make_payload(comment_body="   ")
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "empty" in result.reason.lower()

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

    def test_reject_issue_present_comment_absent(self, event_filter):
        """Valid issue but no comment → filtered (line 101 branch)."""
        payload = {
            "webhookEvent": "comment_created",
            "timestamp": 1700000001,
            "issue": {
                "id": "1",
                "key": "DEFECT-999",
                "fields": {
                    "summary": "Test",
                    "issuetype": {"name": "Bug"},
                    "status": {"name": "Open"},
                },
            },
            # comment key intentionally absent
        }
        event = JiraWebhookEvent(**payload)
        result = event_filter.evaluate(event)
        assert result.accepted is False
        assert "comment" in result.reason.lower()


class TestEventFilterWithSQLiteStore:
    """Tests for the SQLite-backed idempotency path (event_store)."""

    def test_db_backed_idempotency_rejects_duplicate(self, tmp_path):
        """A second EventFilter instance sharing the same SQLiteStore rejects a seen event."""
        from src.storage.sqlite_store import SQLiteStore

        db_file = str(tmp_path / "test_idem.db")
        store = SQLiteStore(db_file)
        ef1 = EventFilter(event_store=store)

        payload = _make_payload(comment_id="db-idem-1", timestamp=1800000001)
        event = JiraWebhookEvent(**payload)

        first = ef1.evaluate(event)
        assert first.accepted is True

        # New filter instance — empty in-memory set, but DB file has the event
        ef2 = EventFilter(event_store=SQLiteStore(db_file))
        second = ef2.evaluate(event)
        assert second.accepted is False
        assert "Duplicate" in second.reason

    def test_db_backed_accepts_different_event(self, tmp_path):
        """Two distinct events are both accepted when store-backed."""
        from src.storage.sqlite_store import SQLiteStore

        store = SQLiteStore(str(tmp_path / "test_two_events.db"))
        ef = EventFilter(event_store=store)

        e1 = JiraWebhookEvent(**_make_payload(comment_id="db-ev-1", timestamp=1800000010))
        e2 = JiraWebhookEvent(**_make_payload(comment_id="db-ev-2", timestamp=1800000011))

        assert ef.evaluate(e1).accepted is True
        assert ef.evaluate(e2).accepted is True
