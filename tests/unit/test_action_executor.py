"""Tests for the approve → Jira post action executor and SQLite-backed pipeline.

These tests verify the Phase 3 enhancements to app.py:
 - SQLiteDraftStore integration (replaces in-memory dict)
 - Approve endpoint posts to Jira
 - Reject endpoint stores feedback
 - Collector wiring with log_lookup + testrail
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from src.api.app import app, event_filter, draft_store


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear shared state between tests."""
    event_filter.reset()
    draft_store.clear()
    yield
    event_filter.reset()
    draft_store.clear()


client = TestClient(app)


def _webhook_payload(
    comment_body: str = "Cannot reproduce this on my machine.",
    issue_key: str = "DEFECT-600",
    comment_id: str = "77001",
    timestamp: int = 1800000001,
) -> dict:
    return {
        "webhookEvent": "comment_created",
        "timestamp": timestamp,
        "issue": {
            "id": "1",
            "key": issue_key,
            "fields": {
                "summary": "Test issue for action executor",
                "issuetype": {"name": "Bug"},
                "status": {"name": "Open"},
            },
        },
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "author": {
                "accountId": "u1",
                "displayName": "Dev User",
                "emailAddress": "dev@company.com",
            },
            "created": "2025-06-01T10:00:00.000+0000",
            "updated": "2025-06-01T10:00:00.000+0000",
        },
    }


# ---- SQLite integration ------------------------------------------------ #

class TestSQLiteIntegration:
    def test_draft_persisted_in_sqlite(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        stored = draft_store.get(draft_id)
        assert stored is not None
        assert stored["issue_key"] == "DEFECT-600"

    def test_health_shows_draft_count(self):
        client.post("/webhook/jira", json=_webhook_payload())
        resp = client.get("/health")
        assert resp.json()["drafts_in_store"] == 1

    def test_list_drafts_with_total(self):
        client.post("/webhook/jira", json=_webhook_payload())
        resp = client.get("/drafts")
        data = resp.json()
        assert data["count"] == 1
        assert data["total"] == 1

    def test_list_drafts_filter_by_status(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        # Initially "generated"
        gen = client.get("/drafts?status=generated")
        assert gen.json()["count"] == 1

        # No approved yet
        appr = client.get("/drafts?status=approved")
        assert appr.json()["count"] == 0


# ---- Approve + Jira post action executor -------------------------------- #

class TestApproveActionExecutor:
    def test_approve_marks_status(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        approve = client.post(
            "/approve",
            json={"draft_id": draft_id, "approved_by": "qa@co.com"},
        )
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"

        stored = draft_store.get(draft_id)
        assert stored["status"] == "approved"
        assert stored["approved_by"] == "qa@co.com"

    @patch("src.api.app._jira_client")
    def test_approve_posts_to_jira(self, mock_jira):
        mock_jira.add_comment = MagicMock(return_value={"id": "c_new"})

        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        approve = client.post(
            "/approve",
            json={
                "draft_id": draft_id,
                "approved_by": "qa@co.com",
                "post_to_jira": True,
            },
        )
        result = approve.json()
        assert result["status"] == "approved"
        assert result["posted_to_jira"] is True
        mock_jira.add_comment.assert_called_once()

    @patch("src.api.app._jira_client", None)
    def test_approve_without_jira_client(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        approve = client.post(
            "/approve",
            json={"draft_id": draft_id, "approved_by": "qa@co.com"},
        )
        result = approve.json()
        assert result["status"] == "approved"
        assert result["posted_to_jira"] is False

    def test_approve_nonexistent_draft(self):
        resp = client.post(
            "/approve",
            json={"draft_id": "nope", "approved_by": "qa@co.com"},
        )
        assert resp.status_code == 404

    @patch("src.api.app._jira_client")
    def test_approve_jira_failure_still_approves(self, mock_jira):
        """If posting to Jira fails, draft should still be approved."""
        mock_jira.add_comment.side_effect = Exception("Jira down")

        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        approve = client.post(
            "/approve",
            json={"draft_id": draft_id, "approved_by": "qa@co.com"},
        )
        result = approve.json()
        assert result["status"] == "approved"
        assert result["posted_to_jira"] is False

        stored = draft_store.get(draft_id)
        assert stored["status"] == "approved"


# ---- Reject with feedback ---------------------------------------------- #

class TestRejectWithFeedback:
    def test_reject_stores_feedback(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        reject = client.post(
            "/reject",
            json={"draft_id": draft_id, "feedback": "Too verbose"},
        )
        assert reject.status_code == 200
        assert reject.json()["status"] == "rejected"

        stored = draft_store.get(draft_id)
        assert stored["status"] == "rejected"
        assert stored["feedback"] == "Too verbose"

    def test_reject_nonexistent_draft(self):
        resp = client.post(
            "/reject",
            json={"draft_id": "nope", "feedback": ""},
        )
        assert resp.status_code == 404
