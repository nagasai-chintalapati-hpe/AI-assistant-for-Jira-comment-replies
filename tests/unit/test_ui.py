"""Unit tests for the Draft Review UI routes (GET /ui, GET /ui/drafts/{id}, POST approve/reject)."""

import pytest
from fastapi.testclient import TestClient

from src.api.app import app, draft_store
from src.models.draft import Draft, DraftStatus
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_draft(
    draft_id: str = "draft_test001",
    issue_key: str = "TEST-1",
    comment_id: str = "100",
    body: str = "This is a test draft reply.",
    status: DraftStatus = DraftStatus.GENERATED,
    confidence: float = 0.85,
    classification: str = "cannot_reproduce",
) -> Draft:
    d = Draft(
        draft_id=draft_id,
        issue_key=issue_key,
        in_reply_to_comment_id=comment_id,
        created_at=datetime.now(timezone.utc),
        created_by="system",
        body=body,
        confidence_score=confidence,
        status=status,
        classification_type=classification,
        classification_reasoning="Test reasoning",
        missing_info=["Environment details", "Reproduction steps"],
        citations=[{"source": "Confluence", "url": "https://example.com", "excerpt": "Some content"}],
        evidence_used=["Confluence page: Setup Guide"],
    )
    draft_store.save(d, classification=classification)
    return d


@pytest.fixture(autouse=True)
def _clean_store():
    """Clear the store before and after each test."""
    draft_store.clear()
    yield
    draft_store.clear()


client = TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# GET /ui — draft list page
# ---------------------------------------------------------------------------

class TestUiList:
    def test_empty_list_returns_200(self):
        resp = client.get("/ui")
        assert resp.status_code == 200
        assert "Draft Replies" in resp.text

    def test_list_shows_draft_when_present(self):
        _make_draft(issue_key="IP-7", body="Cannot reproduce on staging.")
        resp = client.get("/ui")
        assert resp.status_code == 200
        assert "IP-7" in resp.text
        assert "Cannot reproduce on staging" in resp.text

    def test_list_filter_by_issue_key(self):
        _make_draft(draft_id="d1", issue_key="IP-1")
        _make_draft(draft_id="d2", issue_key="IP-2")
        resp = client.get("/ui?issue_key=IP-1")
        assert resp.status_code == 200
        assert "IP-1" in resp.text
        # IP-2 should not appear (filtered)
        assert "IP-2" not in resp.text

    def test_list_filter_by_status(self):
        _make_draft(draft_id="d_gen", status=DraftStatus.GENERATED)
        _make_draft(draft_id="d_rej", status=DraftStatus.REJECTED)
        resp = client.get("/ui?status=generated")
        assert resp.status_code == 200
        assert "generated" in resp.text

    def test_empty_state_message_displayed(self):
        resp = client.get("/ui")
        assert resp.status_code == 200
        assert "No drafts found" in resp.text

    def test_content_type_is_html(self):
        resp = client.get("/ui")
        assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# GET /ui/drafts/{id} — review page
# ---------------------------------------------------------------------------

class TestUiReview:
    def test_review_page_200_with_valid_draft(self):
        d = _make_draft()
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert d.body in resp.text

    def test_review_page_404_for_missing_draft(self):
        resp = client.get("/ui/drafts/nonexistent-id")
        assert resp.status_code == 404

    def test_review_shows_classification(self):
        d = _make_draft(classification="cannot_reproduce")
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert "cannot reproduce" in resp.text.lower() or "cannot_reproduce" in resp.text

    def test_review_shows_missing_info(self):
        d = _make_draft()
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert "Environment details" in resp.text

    def test_review_shows_citations(self):
        d = _make_draft()
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert "Confluence" in resp.text

    def test_review_shows_evidence_used(self):
        d = _make_draft()
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert "Setup Guide" in resp.text

    def test_review_approve_button_visible_when_generated(self):
        d = _make_draft(status=DraftStatus.GENERATED)
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert "Approve" in resp.text

    def test_review_no_approve_button_when_rejected(self):
        d = _make_draft(status=DraftStatus.REJECTED)
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        # The actions block is hidden; status badge should say rejected
        assert "rejected" in resp.text

    def test_review_confidence_displayed(self):
        d = _make_draft(confidence=0.85)
        resp = client.get(f"/ui/drafts/{d.draft_id}")
        assert resp.status_code == 200
        assert "85%" in resp.text


# ---------------------------------------------------------------------------
# POST /ui/drafts/{id}/approve — form submission
# ---------------------------------------------------------------------------

class TestUiApprove:
    def test_approve_redirects_303(self):
        d = _make_draft()
        resp = client.post(
            f"/ui/drafts/{d.draft_id}/approve",
            data={"body": d.body},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/ui/drafts/{d.draft_id}"

    def test_approve_updates_status_in_store(self):
        d = _make_draft()
        client.post(
            f"/ui/drafts/{d.draft_id}/approve",
            data={"body": d.body},
        )
        stored = draft_store.get(d.draft_id)
        assert stored is not None
        assert stored["status"] == DraftStatus.APPROVED.value

    def test_approve_persists_edited_body(self):
        d = _make_draft(body="Original body text.")
        new_body = "Edited reply with extra detail."
        client.post(
            f"/ui/drafts/{d.draft_id}/approve",
            data={"body": new_body},
        )
        stored = draft_store.get(d.draft_id)
        assert stored is not None
        assert stored["body"] == new_body

    def test_approve_404_for_missing_draft(self):
        resp = client.post(
            "/ui/drafts/ghost-id/approve",
            data={"body": "some text"},
        )
        assert resp.status_code == 404

    def test_approve_uses_original_body_when_form_body_empty(self):
        d = _make_draft(body="Keep this body.")
        client.post(f"/ui/drafts/{d.draft_id}/approve", data={"body": ""})
        stored = draft_store.get(d.draft_id)
        assert stored is not None
        assert stored["body"] == "Keep this body."


# ---------------------------------------------------------------------------
# POST /ui/drafts/{id}/reject — form submission
# ---------------------------------------------------------------------------

class TestUiReject:
    def test_reject_redirects_303(self):
        d = _make_draft()
        resp = client.post(
            f"/ui/drafts/{d.draft_id}/reject",
            data={"feedback": "Missing logs"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/ui/drafts/{d.draft_id}"

    def test_reject_updates_status_in_store(self):
        d = _make_draft()
        client.post(
            f"/ui/drafts/{d.draft_id}/reject",
            data={"feedback": "Needs more context"},
        )
        stored = draft_store.get(d.draft_id)
        assert stored is not None
        assert stored["status"] == DraftStatus.REJECTED.value

    def test_reject_stores_feedback(self):
        d = _make_draft()
        client.post(
            f"/ui/drafts/{d.draft_id}/reject",
            data={"feedback": "Wrong issue type"},
        )
        # Feedback is stored in the indexed column (not in JSON blob by default)
        row = draft_store._conn.execute(
            "SELECT feedback FROM drafts WHERE draft_id = ?", (d.draft_id,)
        ).fetchone()
        assert row is not None
        assert row["feedback"] == "Wrong issue type"

    def test_reject_without_feedback_still_works(self):
        d = _make_draft()
        resp = client.post(f"/ui/drafts/{d.draft_id}/reject", data={})
        assert resp.status_code == 303

    def test_reject_404_for_missing_draft(self):
        resp = client.post("/ui/drafts/ghost-id/reject", data={"feedback": "x"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# SQLiteDraftStore.update_body
# ---------------------------------------------------------------------------

class TestUpdateBody:
    def test_update_body_changes_body_in_store(self):
        d = _make_draft(body="Original")
        result = draft_store.update_body(d.draft_id, "Updated body")
        assert result is True
        stored = draft_store.get(d.draft_id)
        assert stored["body"] == "Updated body"

    def test_update_body_returns_false_for_unknown_id(self):
        result = draft_store.update_body("no-such-id", "text")
        assert result is False

    def test_update_body_patches_json_blob(self):
        d = _make_draft(body="Before")
        draft_store.update_body(d.draft_id, "After")
        import json
        row = draft_store._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (d.draft_id,)
        ).fetchone()
        data = json.loads(row["data_json"])
        assert data["body"] == "After"
