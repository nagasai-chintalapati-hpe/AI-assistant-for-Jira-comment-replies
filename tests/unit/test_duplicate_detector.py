"""Unit tests for DuplicateDetector and pattern detection."""

import pytest
from unittest.mock import MagicMock, patch
from src.agent.duplicate_detector import (
    DuplicateDetector,
    DuplicateCheckResult,
    SimilarDraft,
    _tokenize,
    _jaccard,
)


class TestTokenize:
    def test_lowercases(self):
        assert "hello" in _tokenize("Hello World")

    def test_filters_short_words(self):
        tokens = _tokenize("is a an the foo")
        assert "is" not in tokens
        assert "foo" in tokens

    def test_letters_only(self):
        tokens = _tokenize("build #42 passed v1.2.3")
        assert "build" in tokens
        assert "passed" in tokens
        assert "#42" not in tokens

    def test_empty_string(self):
        assert _tokenize("") == set()


class TestJaccard:
    def test_identical_sets(self):
        s = {"foo", "bar", "baz"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint_sets(self):
        assert _jaccard({"foo"}, {"bar"}) == 0.0

    def test_partial_overlap(self):
        a = {"a", "b", "c"}
        b = {"b", "c", "d"}
        assert _jaccard(a, b) == pytest.approx(0.5)

    def test_empty_sets(self):
        assert _jaccard(set(), {"foo"}) == 0.0
        assert _jaccard(set(), set()) == 0.0


def _make_store(drafts: list[dict]):
    """Return a mock SQLiteDraftStore."""
    store = MagicMock()
    store.find_recent_by_issue.return_value = drafts
    return store


class TestDuplicateDetector:
    def test_no_past_drafts_returns_empty(self):
        detector = DuplicateDetector(threshold=0.25)
        store = _make_store([])
        result = detector.check("cannot reproduce this bug", "PROJ-1", store)
        assert isinstance(result, DuplicateCheckResult)
        assert result.similar_drafts == []
        assert not result.is_likely_duplicate

    def test_high_overlap_detected(self):
        past = [
            {
                "draft_id": "draft_001",
                "issue_key": "PROJ-1",
                "status": "approved",
                "body": "Thanks for reporting. Cannot reproduce this bug on our end.",
                "created_at": "2025-01-01T10:00:00+00:00",
            }
        ]
        detector = DuplicateDetector(threshold=0.25)
        store = _make_store(past)
        result = detector.check(
            "cannot reproduce this bug on my machine",
            "PROJ-1",
            store,
        )
        assert result.is_likely_duplicate
        assert len(result.similar_drafts) == 1
        assert result.similar_drafts[0].draft_id == "draft_001"
        assert result.similar_drafts[0].similarity > 0.25

    def test_low_overlap_not_detected(self):
        past = [
            {
                "draft_id": "draft_002",
                "issue_key": "PROJ-1",
                "status": "generated",
                "body": "Completely unrelated content about something else entirely.",
                "created_at": "2025-01-01T10:00:00+00:00",
            }
        ]
        detector = DuplicateDetector(threshold=0.25)
        store = _make_store(past)
        result = detector.check("cannot reproduce the login error", "PROJ-1", store)
        assert not result.is_likely_duplicate

    def test_results_sorted_by_similarity_descending(self):
        past = [
            {
                "draft_id": "draft_low",
                "issue_key": "PROJ-1",
                "status": "generated",
                "body": "reproduce login timeout error environment",
                "created_at": "2025-01-01T10:00:00+00:00",
            },
            {
                "draft_id": "draft_high",
                "issue_key": "PROJ-1",
                "status": "approved",
                "body": "cannot reproduce login timeout error on staging environment version",
                "created_at": "2025-01-02T10:00:00+00:00",
            },
        ]
        detector = DuplicateDetector(threshold=0.1)
        store = _make_store(past)
        result = detector.check(
            "cannot reproduce login timeout error staging environment",
            "PROJ-1",
            store,
        )
        assert len(result.similar_drafts) >= 2
        sims = [s.similarity for s in result.similar_drafts]
        assert sims == sorted(sims, reverse=True)

    def test_limit_caps_results(self):
        past = [
            {
                "draft_id": f"draft_{i:03d}",
                "issue_key": "PROJ-1",
                "status": "generated",
                "body": "cannot reproduce this error on the staging environment build version",
                "created_at": "2025-01-01T10:00:00+00:00",
            }
            for i in range(10)
        ]
        detector = DuplicateDetector(threshold=0.1)
        store = _make_store(past)
        result = detector.check(
            "cannot reproduce error staging environment build",
            "PROJ-1",
            store,
            limit=3,
        )
        assert len(result.similar_drafts) <= 3

    def test_to_dict_list_serialises_correctly(self):
        past = [
            {
                "draft_id": "draft_abc",
                "issue_key": "PROJ-1",
                "status": "rejected",
                "body": "Cannot reproduce this reported bug on staging.",
                "created_at": "2025-06-01T09:00:00+00:00",
            }
        ]
        detector = DuplicateDetector(threshold=0.1)
        store = _make_store(past)
        result = detector.check("cannot reproduce reported bug", "PROJ-1", store)
        if result.is_likely_duplicate:
            dicts = result.to_dict_list()
            assert isinstance(dicts, list)
            assert dicts[0]["draft_id"] == "draft_abc"
            assert "similarity" in dicts[0]
            assert "body_preview" in dicts[0]
            assert "created_at" in dicts[0]

    def test_empty_result_to_dict_list(self):
        result = DuplicateCheckResult(similar_drafts=[])
        assert result.to_dict_list() == []

    def test_body_preview_truncated(self):
        long_body = "word " * 100
        past = [
            {
                "draft_id": "draft_long",
                "issue_key": "PROJ-1",
                "status": "generated",
                "body": long_body,
                "created_at": "2025-01-01T10:00:00+00:00",
            }
        ]
        detector = DuplicateDetector(threshold=0.01)
        store = _make_store(past)
        result = detector.check("word " * 5, "PROJ-1", store)
        if result.similar_drafts:
            assert len(result.similar_drafts[0].body_preview) <= 120

    def test_missing_body_field_handled_gracefully(self):
        past = [{"draft_id": "draft_x", "issue_key": "PROJ-1", "status": "generated"}]
        detector = DuplicateDetector(threshold=0.1)
        store = _make_store(past)
        result = detector.check("some comment body", "PROJ-1", store)
        assert isinstance(result, DuplicateCheckResult)


class TestDraftModelFields:
    def test_draft_accepts_similar_drafts(self):
        from src.models.draft import Draft, DraftStatus
        from datetime import datetime, timezone

        d = Draft(
            draft_id="draft_001",
            issue_key="PROJ-1",
            in_reply_to_comment_id="c1",
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body="Test body",
            confidence_score=0.9,
            status=DraftStatus.GENERATED,
            similar_drafts=[
                {
                    "draft_id": "draft_old",
                    "issue_key": "PROJ-1",
                    "status": "approved",
                    "similarity": 0.45,
                    "body_preview": "Old reply preview",
                    "created_at": "2025-01-01T10:00:00",
                }
            ],
            pattern_note="Pattern detected: 4 open issues on v2.3.1 — possible systemic issue.",
        )
        assert d.similar_drafts is not None
        assert len(d.similar_drafts) == 1
        assert d.similar_drafts[0]["similarity"] == 0.45
        assert d.pattern_note is not None
        assert "4 open issues" in d.pattern_note

    def test_draft_defaults_none(self):
        from src.models.draft import Draft, DraftStatus
        from datetime import datetime, timezone

        d = Draft(
            draft_id="draft_002",
            issue_key="PROJ-2",
            in_reply_to_comment_id="c2",
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body="Body",
            confidence_score=0.8,
            status=DraftStatus.GENERATED,
        )
        assert d.similar_drafts is None
        assert d.pattern_note is None
