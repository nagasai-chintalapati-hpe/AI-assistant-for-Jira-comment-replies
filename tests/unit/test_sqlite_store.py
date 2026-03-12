"""Tests for SQLite-backed draft store."""

import pytest
from datetime import datetime, timezone

from src.models.draft import Draft, DraftStatus
from src.storage.sqlite_store import SQLiteDraftStore


@pytest.fixture
def store():
    """In-memory SQLite store for testing."""
    s = SQLiteDraftStore(db_path=":memory:")
    yield s
    s.close()


@pytest.fixture
def sample_draft():
    """A minimal Draft for testing."""
    return Draft(
        draft_id="draft_001",
        issue_key="DEFECT-100",
        in_reply_to_comment_id="10001",
        created_at=datetime.now(timezone.utc),
        created_by="system",
        body="Thanks for the update. We're investigating.",
        confidence_score=0.85,
        status=DraftStatus.GENERATED,
        suggested_labels=["needs-info"],
        citations=[{"source": "Jenkins", "url": "https://ci.example.com/job/1"}],
    )


@pytest.fixture
def second_draft():
    """A second Draft with a different issue key."""
    return Draft(
        draft_id="draft_002",
        issue_key="DEFECT-200",
        in_reply_to_comment_id="10002",
        created_at=datetime.now(timezone.utc),
        created_by="system",
        body="A fix has been deployed.",
        confidence_score=0.90,
        status=DraftStatus.GENERATED,
    )


# Save & retrieve

class TestSaveAndGet:
    def test_save_and_get(self, store, sample_draft):
        store.save(sample_draft, classification="cannot_reproduce")
        result = store.get("draft_001")
        assert result is not None
        assert result["draft_id"] == "draft_001"
        assert result["issue_key"] == "DEFECT-100"
        assert result["body"] == sample_draft.body

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("draft_999") is None

    def test_save_preserves_citations(self, store, sample_draft):
        store.save(sample_draft)
        result = store.get("draft_001")
        assert result["citations"] == [
            {"source": "Jenkins", "url": "https://ci.example.com/job/1"}
        ]

    def test_save_preserves_labels(self, store, sample_draft):
        store.save(sample_draft)
        result = store.get("draft_001")
        assert result["suggested_labels"] == ["needs-info"]

    def test_upsert_replaces_existing(self, store, sample_draft):
        store.save(sample_draft)
        updated = sample_draft.model_copy(update={"body": "Updated body"})
        store.save(updated)
        result = store.get("draft_001")
        assert result["body"] == "Updated body"


# Listing & filtering

class TestListAndFilter:
    def test_list_all_returns_all(self, store, sample_draft, second_draft):
        store.save(sample_draft)
        store.save(second_draft)
        results = store.list_all()
        assert len(results) == 2

    def test_filter_by_issue_key(self, store, sample_draft, second_draft):
        store.save(sample_draft)
        store.save(second_draft)
        results = store.list_all(issue_key="DEFECT-100")
        assert len(results) == 1
        assert results[0]["issue_key"] == "DEFECT-100"

    def test_filter_by_status(self, store, sample_draft, second_draft):
        store.save(sample_draft)
        store.save(second_draft)
        store.update_status("draft_002", DraftStatus.APPROVED, approved_by="qa@test.com")
        results = store.list_all(status="approved")
        assert len(results) == 1
        assert results[0]["draft_id"] == "draft_002"

    def test_list_empty_store(self, store):
        results = store.list_all()
        assert results == []

    def test_count(self, store, sample_draft, second_draft):
        store.save(sample_draft)
        store.save(second_draft)
        assert store.count() == 2
        assert store.count(issue_key="DEFECT-100") == 1

    def test_pagination(self, store, sample_draft, second_draft):
        store.save(sample_draft)
        store.save(second_draft)
        page1 = store.list_all(limit=1, offset=0)
        page2 = store.list_all(limit=1, offset=1)
        assert len(page1) == 1
        assert len(page2) == 1
        assert page1[0]["draft_id"] != page2[0]["draft_id"]


# Status updates

class TestStatusUpdates:
    def test_approve_draft(self, store, sample_draft):
        store.save(sample_draft)
        result = store.update_status(
            "draft_001", DraftStatus.APPROVED, approved_by="qa@test.com"
        )
        assert result is True
        draft = store.get("draft_001")
        assert draft["status"] == "approved"
        assert draft["approved_by"] == "qa@test.com"
        assert draft["approved_at"] is not None

    def test_reject_draft_with_feedback(self, store, sample_draft):
        store.save(sample_draft)
        result = store.update_status(
            "draft_001", DraftStatus.REJECTED, feedback="Tone needs adjustment"
        )
        assert result is True
        draft = store.get("draft_001")
        assert draft["status"] == "rejected"
        assert draft["feedback"] == "Tone needs adjustment"

    def test_update_nonexistent_returns_false(self, store):
        result = store.update_status("draft_999", DraftStatus.APPROVED)
        assert result is False

    def test_mark_posted(self, store, sample_draft):
        store.save(sample_draft)
        result = store.mark_posted("draft_001")
        assert result is True
        draft = store.get("draft_001")
        assert draft["posted_at"] is not None

    def test_mark_posted_nonexistent_returns_false(self, store):
        assert store.mark_posted("draft_999") is False


# Delete

class TestDelete:
    def test_delete_existing(self, store, sample_draft):
        store.save(sample_draft)
        assert store.delete("draft_001") is True
        assert store.get("draft_001") is None

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete("draft_999") is False
