"""Tests for comment classifier – keyword fallback path (no Copilot SDK in CI)."""

import pytest
from datetime import datetime, timezone
from src.models.comment import Comment
from src.models.classification import CommentType
from src.agent.classifier import CommentClassifier


@pytest.fixture
def classifier():
    # No api_key → keyword-only mode
    return CommentClassifier()


def _make_comment(body: str, comment_id: str = "10000") -> Comment:
    return Comment(
        comment_id=comment_id,
        issue_key="DEFECT-123",
        author="dev@company.com",
        author_role="Developer",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body=body,
    )


class TestKeywordClassification:
    def test_cannot_reproduce(self, classifier):
        result = classifier.classify(_make_comment("Cannot reproduce this on my machine."))
        assert result.comment_type == CommentType.CANNOT_REPRODUCE
        assert result.confidence >= 0.8
        assert result.missing_context is not None

    def test_cannot_repro_variant(self, classifier):
        result = classifier.classify(_make_comment("I can't repro this in staging."))
        assert result.comment_type == CommentType.CANNOT_REPRODUCE

    def test_need_logs(self, classifier):
        result = classifier.classify(
            _make_comment("Can you provide the error logs from the crash?")
        )
        assert result.comment_type == CommentType.NEED_MORE_INFO
        assert result.confidence >= 0.7

    def test_need_more_info(self, classifier):
        result = classifier.classify(
            _make_comment("We need more info about the stack trace")
        )
        assert result.comment_type == CommentType.NEED_MORE_INFO

    def test_as_designed(self, classifier):
        result = classifier.classify(
            _make_comment("This is as designed. See the documentation.")
        )
        assert result.comment_type == CommentType.BY_DESIGN

    def test_by_design(self, classifier):
        result = classifier.classify(
            _make_comment("This is by design per the spec.")
        )
        assert result.comment_type == CommentType.BY_DESIGN

    def test_already_fixed(self, classifier):
        result = classifier.classify(
            _make_comment("Already fixed in build 2.3.1")
        )
        assert result.comment_type == CommentType.FIXED_VALIDATE

    def test_fix_ready(self, classifier):
        result = classifier.classify(
            _make_comment("Fix ready in v2.5. Please validate.")
        )
        assert result.comment_type == CommentType.FIXED_VALIDATE

    def test_other_fallback(self, classifier):
        result = classifier.classify(
            _make_comment("Looks good to me, thanks for the quick turnaround!")
        )
        assert result.comment_type == CommentType.OTHER
        assert result.confidence <= 0.6

    def test_suggested_questions_for_cannot_reproduce(self, classifier):
        result = classifier.classify(
            _make_comment("Unable to reproduce this issue.")
        )
        assert result.suggested_questions is not None
        assert len(result.suggested_questions) > 0
