"""Tests for response drafter – template path (no Copilot SDK in CI)."""

import pytest
from datetime import datetime, timezone
from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
from src.models.context import IssueContext, ContextCollectionResult
from src.models.draft import DraftStatus
from src.agent.drafter import ResponseDrafter, TEMPLATES


@pytest.fixture
def drafter():
    # No api_key → template-only mode
    return ResponseDrafter()


@pytest.fixture
def sample_context():
    issue_context = IssueContext(
        issue_key="DEFECT-123",
        summary="UI crashes on Chrome when uploading large file",
        description="Upload fails with error 500",
        issue_type="Bug",
        status="Open",
        priority="High",
        environment="Staging, Chrome 121",
        versions=["1.8.14"],
        components=["Upload Service"],
    )
    return ContextCollectionResult(
        issue_context=issue_context,
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=100.0,
    )


def _make_classification(
    ctype: CommentType,
    confidence: float = 0.9,
    missing: list[str] | None = None,
) -> CommentClassification:
    return CommentClassification(
        comment_id="10000",
        comment_type=ctype,
        confidence=confidence,
        reasoning="test",
        missing_context=missing,
    )


class TestDraftGeneration:
    def test_draft_for_cannot_reproduce(self, drafter, sample_comment, sample_context):
        classification = _make_classification(
            CommentType.CANNOT_REPRODUCE, missing=["Environment details"]
        )
        draft = drafter.draft(sample_comment, classification, sample_context)

        assert draft.draft_id.startswith("draft_")
        assert draft.issue_key == "DEFECT-123"
        assert draft.status == DraftStatus.GENERATED
        assert len(draft.body) > 0
        assert "reproduce" in draft.body.lower() or "confirm" in draft.body.lower()

    def test_draft_for_need_more_info(self, drafter, sample_comment, sample_context):
        classification = _make_classification(
            CommentType.NEED_MORE_INFO, missing=["Logs", "Correlation ID"]
        )
        draft = drafter.draft(sample_comment, classification, sample_context)
        assert "missing" in draft.body.lower() or "provide" in draft.body.lower()

    def test_draft_for_by_design(self, drafter, sample_comment, sample_context):
        classification = _make_classification(CommentType.BY_DESIGN)
        draft = drafter.draft(sample_comment, classification, sample_context)
        assert "expected behavior" in draft.body.lower() or "design" in draft.body.lower()

    def test_draft_for_fixed_validate(self, drafter, sample_comment, sample_context):
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, sample_context)
        assert "fix" in draft.body.lower()
        assert "1.8.14" in draft.body  # version from context

    def test_draft_for_other(self, drafter, sample_comment, sample_context):
        classification = _make_classification(CommentType.OTHER)
        draft = drafter.draft(sample_comment, classification, sample_context)
        assert draft.body  # non-empty
        assert "DEFECT-123" in draft.body


class TestSuggestedLabels:
    def test_labels_for_cannot_reproduce(self, drafter, sample_comment, sample_context):
        classification = _make_classification(
            CommentType.CANNOT_REPRODUCE, missing=["env"]
        )
        draft = drafter.draft(sample_comment, classification, sample_context)
        assert "cannot-reproduce" in draft.suggested_labels
        assert "needs-info" in draft.suggested_labels

    def test_labels_for_fixed_validate(self, drafter, sample_comment, sample_context):
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, sample_context)
        assert "fixed-validate" in draft.suggested_labels


class TestSuggestedActions:
    def test_actions_for_fixed_validate(self, drafter, sample_comment, sample_context):
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, sample_context)
        action_types = [a["action"] for a in draft.suggested_actions]
        assert "transition" in action_types


class TestTemplatesExist:
    """Every CommentType used in the drafter should have a template."""

    def test_all_types_have_templates(self):
        for ctype in CommentType:
            assert ctype in TEMPLATES, f"Missing template for {ctype}"
