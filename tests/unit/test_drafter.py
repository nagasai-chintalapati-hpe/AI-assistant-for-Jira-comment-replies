"""Tests for response drafter – template-based generation."""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
from src.models.context import IssueContext, ContextCollectionResult
from src.models.draft import DraftStatus
from src.agent.drafter import ResponseDrafter, TEMPLATES


@pytest.fixture
def drafter():
    """Create drafter without API key (template-only mode)."""
    return ResponseDrafter()


@pytest.fixture
def sample_context():
    """Create sample context for testing."""
    issue_context = IssueContext(
        issue_key="DEFECT-123",
        summary="UI crashes on Chrome when uploading large file",
        description="Upload fails with error 500",
        issue_type="Bug",
        status="Open",
        priority="High",
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
    """Helper to create test classification."""
    return CommentClassification(
        comment_id="10000",
        comment_type=ctype,
        confidence=confidence,
        reasoning="test",
        missing_context=missing,
    )


class TestDraftGeneration:
    @pytest.mark.asyncio
    async def test_draft_for_cannot_reproduce(self, drafter, sample_comment, sample_context):
        """Test draft generation for CANNOT_REPRODUCE."""
        classification = _make_classification(
            CommentType.CANNOT_REPRODUCE, missing=["Environment details"]
        )
        draft = await drafter.draft(sample_comment, classification, sample_context)

        assert draft.draft_id.startswith("draft_")
        assert draft.issue_key == "DEFECT-123"
        assert draft.status == DraftStatus.GENERATED
        assert len(draft.body) > 0

    @pytest.mark.asyncio
    async def test_draft_for_need_more_info(self, drafter, sample_comment, sample_context):
        """Test draft generation for NEED_MORE_INFO."""
        classification = _make_classification(
            CommentType.NEED_MORE_INFO, missing=["Logs", "Correlation ID"]
        )
        draft = await drafter.draft(sample_comment, classification, sample_context)
        assert len(draft.body) > 0

    @pytest.mark.asyncio
    async def test_draft_for_by_design(self, drafter, sample_comment, sample_context):
        """Test draft generation for BY_DESIGN."""
        classification = _make_classification(CommentType.BY_DESIGN)
        draft = await drafter.draft(sample_comment, classification, sample_context)
        assert len(draft.body) > 0

    @pytest.mark.asyncio
    async def test_draft_for_fixed_validate(self, drafter, sample_comment, sample_context):
        """Test draft generation for FIXED_VALIDATE."""
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = await drafter.draft(sample_comment, classification, sample_context)
        assert "fix" in draft.body.lower() or "deploy" in draft.body.lower()

    @pytest.mark.asyncio
    async def test_draft_for_other(self, drafter, sample_comment, sample_context):
        """Test draft generation for OTHER classification."""
        classification = _make_classification(CommentType.OTHER)
        draft = await drafter.draft(sample_comment, classification, sample_context)
        assert draft.body
        assert "DEFECT-123" in draft.body


class TestSuggestedActions:
    @pytest.mark.asyncio
    async def test_actions_for_fixed_validate(self, drafter, sample_comment, sample_context):
        """Test suggested actions for FIXED_VALIDATE."""
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = await drafter.draft(sample_comment, classification, sample_context)
        assert len(draft.suggested_actions) > 0

    @pytest.mark.asyncio
    async def test_actions_for_need_more_info(self, drafter, sample_comment, sample_context):
        """Test suggested actions for NEED_MORE_INFO."""
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = await drafter.draft(sample_comment, classification, sample_context)
        assert len(draft.suggested_actions) > 0


class TestCopilotRefinementPaths:
    @pytest.mark.asyncio
    async def test_refinement_applied_when_available(self, sample_comment, sample_context):
        drafter = ResponseDrafter()
        drafter._client = MagicMock()

        async def _refined(_text):
            return "Refined response text"

        drafter._refine_with_copilot = _refined

        draft = await drafter.draft(
            sample_comment,
            _make_classification(CommentType.NEED_MORE_INFO),
            sample_context,
        )

        assert draft.body == "Refined response text"

    @pytest.mark.asyncio
    async def test_refinement_failure_keeps_template_content(self, sample_comment, sample_context):
        drafter = ResponseDrafter()
        drafter._client = MagicMock()

        async def _none(_text):
            return None

        drafter._refine_with_copilot = _none

        draft = await drafter.draft(
            sample_comment,
            _make_classification(CommentType.BY_DESIGN),
            sample_context,
        )

        assert "expected behavior" in draft.body.lower()

    @pytest.mark.asyncio
    async def test_refine_with_copilot_handles_client_error(self):
        drafter = ResponseDrafter()
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("boom")
        drafter._client = mock_client

        refined = await drafter._refine_with_copilot("Hello")

        assert refined is None


class TestTemplatesExist:
    """Verify all CommentTypes have templates."""

    def test_all_types_have_templates(self):
        """All classification types should have response templates."""
        for ctype in CommentType:
            assert ctype in TEMPLATES, f"Missing template for {ctype}"

