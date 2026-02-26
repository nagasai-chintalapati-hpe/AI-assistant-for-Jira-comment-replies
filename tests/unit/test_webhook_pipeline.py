"""Tests for webhook event handling pipeline."""

from datetime import datetime

from fastapi.testclient import TestClient

import src.api.app as app_module
from src.models.context import ContextCollectionResult, IssueContext


class DummyContextCollector:
    """Test double to avoid Jira environment dependencies."""

    def collect(self, issue_key: str) -> ContextCollectionResult:
        issue_context = IssueContext(
            issue_key=issue_key,
            summary="",
            description="",
            issue_type="",
            status="",
            priority="",
        )
        return ContextCollectionResult(
            issue_context=issue_context,
            rag_results=[],
            available_logs=[],
            collection_timestamp=datetime.utcnow(),
            collection_duration_ms=0.0,
        )


def _sample_payload() -> dict:
    return {
        "webhookEvent": "comment_created",
        "issue": {"key": "DEFECT-123"},
        "comment": {
            "id": "10000",
            "body": "Cannot reproduce on my machine.",
            "created": "2026-02-26T10:30:00.000+0000",
            "updated": "2026-02-26T10:30:00.000+0000",
            "author": {"displayName": "Dev User", "emailAddress": "dev@company.com"},
        },
    }


def test_webhook_comment_created(monkeypatch):
    """Ensure comment_created events are classified and drafted."""
    monkeypatch.setattr(app_module, "ContextCollector", DummyContextCollector)

    client = TestClient(app_module.app)
    response = client.post("/webhook/jira", json=_sample_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "drafted"
    assert body["event"] == "comment_created"
    assert body["issue_key"] == "DEFECT-123"
    assert body["comment_id"] == "10000"
    assert "draft_id" in body
    assert "body" in body


def test_webhook_comment_updated(monkeypatch):
    """Ensure comment_updated events use the same pipeline."""
    monkeypatch.setattr(app_module, "ContextCollector", DummyContextCollector)

    payload = _sample_payload()
    payload["webhookEvent"] = "comment_updated"

    client = TestClient(app_module.app)
    response = client.post("/webhook/jira", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "drafted"
    assert body["event"] == "comment_updated"


def test_webhook_missing_ids(monkeypatch):
    """Missing issue/comment identifiers should be ignored."""
    monkeypatch.setattr(app_module, "ContextCollector", DummyContextCollector)

    payload = {"webhookEvent": "comment_created", "issue": {}, "comment": {}}

    client = TestClient(app_module.app)
    response = client.post("/webhook/jira", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["event"] == "comment_created"
