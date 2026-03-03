"""Tests for comment classifier – keyword fallback path."""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
from src.agent.classifier import CommentClassifier


@pytest.fixture
def classifier():
    """Create classifier without API key (keyword-only mode)."""
    return CommentClassifier()


def _make_comment(body: str, comment_id: str = "10000") -> Comment:
    """Helper to create test comment."""
    return Comment(
        comment_id=comment_id,
        issue_key="DEFECT-123",
        author="dev@company.com",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body=body,
    )


class TestKeywordClassification:
    @pytest.mark.asyncio
    async def test_cannot_reproduce(self, classifier):
        """Test CANNOT_REPRODUCE classification."""
        result = await classifier.classify(_make_comment("Cannot reproduce this on my machine."))
        assert result.comment_type == CommentType.CANNOT_REPRODUCE
        assert result.confidence >= 0.8

    @pytest.mark.asyncio
    async def test_cannot_repro_variant(self, classifier):
        """Test variant keywords for CANNOT_REPRODUCE."""
        result = await classifier.classify(_make_comment("I can't repro this in staging."))
        assert result.comment_type == CommentType.CANNOT_REPRODUCE

    @pytest.mark.asyncio
    async def test_need_more_info(self, classifier):
        """Test NEED_MORE_INFO classification."""
        result = await classifier.classify(
            _make_comment("Can you provide the error logs from the crash?")
        )
        assert result.comment_type == CommentType.NEED_MORE_INFO
        assert result.confidence >= 0.7

    @pytest.mark.asyncio
    async def test_by_design(self, classifier):
        """Test BY_DESIGN classification."""
        result = await classifier.classify(
            _make_comment("This is by design per the spec.")
        )
        assert result.comment_type == CommentType.BY_DESIGN

    @pytest.mark.asyncio
    async def test_fixed_validate(self, classifier):
        """Test FIXED_VALIDATE classification."""
        result = await classifier.classify(
            _make_comment("Already fixed in build 2.3.1")
        )
        assert result.comment_type == CommentType.FIXED_VALIDATE

    @pytest.mark.asyncio
    async def test_other_fallback(self, classifier):
        """Test fallback to OTHER classification."""
        result = await classifier.classify(
            _make_comment("Looks good to me, thanks for the quick turnaround!")
        )
        assert result.comment_type == CommentType.OTHER
        assert result.confidence <= 0.6

    @pytest.mark.asyncio
    async def test_suggested_questions(self, classifier):
        """Test that suggested questions are provided."""
        result = await classifier.classify(
            _make_comment("Unable to reproduce this issue.")
        )
        assert result.suggested_questions is not None
        assert len(result.suggested_questions) > 0


class TestCopilotClassificationPaths:
    @pytest.mark.asyncio
    async def test_copilot_success_used(self):
        classifier = CommentClassifier()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(
                    content='{"comment_type":"by_design","confidence":0.91,"reasoning":"Expected behavior"}'
                )
            )
        ]
        mock_client.chat.completions.create.return_value = mock_response
        classifier._client = mock_client

        result = await classifier.classify(_make_comment("This is expected behavior."))

        assert result.comment_type == CommentType.BY_DESIGN
        assert result.confidence == pytest.approx(0.91)

    @pytest.mark.asyncio
    async def test_copilot_malformed_json_falls_back_to_keywords(self):
        classifier = CommentClassifier()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="not json"))]
        mock_client.chat.completions.create.return_value = mock_response
        classifier._client = mock_client

        result = await classifier.classify(_make_comment("Cannot reproduce on my machine."))

        assert result.comment_type == CommentType.CANNOT_REPRODUCE

    @pytest.mark.asyncio
    async def test_low_confidence_copilot_result_falls_back(self):
        classifier = CommentClassifier()

        async def _low_confidence(_comment):
            return CommentClassification(
                comment_id="10000",
                comment_type=CommentType.OTHER,
                confidence=0.2,
                reasoning="unsure",
            )

        classifier._classify_with_copilot = _low_confidence
        classifier._client = MagicMock()

        result = await classifier.classify(_make_comment("Need logs from staging"))

        assert result.comment_type == CommentType.NEED_MORE_INFO

