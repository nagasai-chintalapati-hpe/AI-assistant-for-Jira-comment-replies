"""Tests for response drafter"""

import pytest
from datetime import datetime
from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
from src.models.context import IssueContext, ContextCollectionResult
from src.models.draft import DraftStatus
from src.agent.drafter import ResponseDrafter


@pytest.fixture
def drafter():
    return ResponseDrafter()


@pytest.fixture
def sample_classification():
    return CommentClassification(
        comment_id="10000",
        comment_type=CommentType.CANNOT_REPRODUCE,
        confidence=0.9,
        reasoning="Developer cannot reproduce",
        missing_context=["Environment details"],
    )


@pytest.fixture
def sample_context():
    issue_context = IssueContext(
        issue_key="DEFECT-123",
        summary="Test issue",
        description="Test description",
        issue_type="Bug",
        status="Open",
        priority="High",
    )
    
    return ContextCollectionResult(
        issue_context=issue_context,
        collection_timestamp=datetime.utcnow(),
        collection_duration_ms=100.0,
    )


def test_draft_generation(drafter, sample_comment, sample_classification, sample_context):
    """Test draft generation"""
    draft = drafter.draft(sample_comment, sample_classification, sample_context)
    
    assert draft.draft_id is not None
    assert draft.issue_key == "DEFECT-123"
    assert draft.status == DraftStatus.GENERATED
    assert len(draft.body) > 0


def test_suggested_labels(drafter, sample_comment, sample_classification, sample_context):
    """Test label suggestion"""
    draft = drafter.draft(sample_comment, sample_classification, sample_context)
    
    assert "needs-info" in draft.suggested_labels
