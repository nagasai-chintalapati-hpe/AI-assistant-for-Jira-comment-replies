"""Integration-style tests for the full webhook → draft pipeline via FastAPI TestClient."""

import pytest
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
    event_type: str = "comment_created",
    issue_type: str = "Bug",
    status: str = "Open",
    comment_body: str = "Cannot reproduce this on my machine.",
    issue_key: str = "DEFECT-500",
    comment_id: str = "99001",
    timestamp: int = 1700000001,
) -> dict:
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
                "accountId": "u1",
                "displayName": "Dev User",
                "emailAddress": "dev@company.com",
            },
            "created": "2025-02-23T10:30:00.000+0000",
            "updated": "2025-02-23T10:30:00.000+0000",
        },
    }


# ---- health check ------------------------------------------------------ #

class TestHealthCheck:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data


# ---- webhook endpoint -------------------------------------------------- #

class TestWebhookEndpoint:
    def test_accepted_event_returns_processed(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"
        assert data["classification"] == "cannot_reproduce"
        assert data["draft_id"].startswith("draft_")

    def test_filtered_event_returns_reason(self):
        payload = _webhook_payload(issue_type="Story")
        resp = client.post("/webhook/jira", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "filtered"

    def test_idempotent_duplicate_rejected(self):
        payload = _webhook_payload()
        first = client.post("/webhook/jira", json=payload)
        assert first.json()["status"] == "processed"

        second = client.post("/webhook/jira", json=payload)
        assert second.json()["status"] == "filtered"
        assert "Duplicate" in second.json()["reason"]

    def test_invalid_json_returns_400(self):
        resp = client.post(
            "/webhook/jira",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_need_logs_classification(self):
        payload = _webhook_payload(
            comment_body="We need logs from staging to investigate further.",
            comment_id="99002",
            timestamp=1700000002,
        )
        resp = client.post("/webhook/jira", json=payload)
        data = resp.json()
        assert data["status"] == "processed"
        assert data["classification"] == "need_more_info"

    def test_fix_ready_classification(self):
        payload = _webhook_payload(
            comment_body="Fix deployed in build 2.4.0. Please validate.",
            comment_id="99003",
            timestamp=1700000003,
        )
        resp = client.post("/webhook/jira", json=payload)
        data = resp.json()
        assert data["classification"] == "fixed_validate"


# ---- draft store ------------------------------------------------------- #

class TestDraftStore:
    def test_draft_stored_after_processing(self):
        client.post("/webhook/jira", json=_webhook_payload())
        assert len(draft_store) == 1

    def test_get_draft_by_id(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        get_resp = client.get(f"/drafts/{draft_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["issue_key"] == "DEFECT-500"

    def test_get_draft_not_found(self):
        resp = client.get("/drafts/nonexistent")
        assert resp.status_code == 404

    def test_list_drafts(self):
        client.post("/webhook/jira", json=_webhook_payload())
        resp = client.get("/drafts")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_list_drafts_filter_by_issue_key(self):
        client.post("/webhook/jira", json=_webhook_payload())
        resp = client.get("/drafts?issue_key=DEFECT-500")
        assert resp.json()["count"] == 1

        resp2 = client.get("/drafts?issue_key=OTHER-1")
        assert resp2.json()["count"] == 0


# ---- approval ---------------------------------------------------------- #

class TestApproval:
    def test_approve_draft(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        approve_resp = client.post(
            "/approve",
            json={"draft_id": draft_id, "approved_by": "qa@company.com"},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"
        assert draft_store[draft_id]["status"] == "approved"

    def test_approve_nonexistent_draft(self):
        resp = client.post(
            "/approve",
            json={"draft_id": "nope", "approved_by": "qa@company.com"},
        )
        assert resp.status_code == 404

    def test_reject_draft(self):
        resp = client.post("/webhook/jira", json=_webhook_payload())
        draft_id = resp.json()["draft_id"]

        reject_resp = client.post(
            "/reject",
            json={"draft_id": draft_id, "feedback": "Needs more detail"},
        )
        assert reject_resp.status_code == 200
        assert reject_resp.json()["status"] == "rejected"
        assert draft_store[draft_id]["status"] == "rejected"

    def test_reject_nonexistent_draft(self):
        resp = client.post(
            "/reject",
            json={"draft_id": "nope", "feedback": ""},
        )
        assert resp.status_code == 404
