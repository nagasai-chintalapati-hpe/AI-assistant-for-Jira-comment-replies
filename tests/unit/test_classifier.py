"""Tests for comment classifier"""

import pytest
from datetime import datetime
from src.models.comment import Comment
from src.models.classification import CommentType
from src.agent.classifier import CommentClassifier


@pytest.fixture
def classifier():
    return CommentClassifier()


def test_classify_cannot_reproduce(classifier):
    """Test classification of 'cannot reproduce' comment"""
    comment = Comment(
        comment_id="10000",
        issue_key="DEFECT-123",
        author="dev@company.com",
        author_role="Developer",
        created=datetime.utcnow(),
        updated=datetime.utcnow(),
        body="Cannot reproduce this on my machine.",
    )
    
    result = classifier.classify(comment)
    
    assert result.comment_type == CommentType.CANNOT_REPRODUCE
    assert result.confidence > 0.8
    assert result.missing_context is not None


def test_classify_need_logs(classifier):
    """Test classification of 'need logs' comment"""
    comment = Comment(
        comment_id="10001",
        issue_key="DEFECT-123",
        author="dev@company.com",
        author_role="Developer",
        created=datetime.utcnow(),
        updated=datetime.utcnow(),
        body="Can you provide the error logs from the crash?",
    )
    
    result = classifier.classify(comment)
    
    assert result.comment_type == CommentType.NEED_MORE_INFO
    assert result.confidence > 0.7


def test_classify_as_designed(classifier):
    """Test classification of 'as designed' comment"""
    comment = Comment(
        comment_id="10002",
        issue_key="DEFECT-123",
        author="dev@company.com",
        author_role="Developer",
        created=datetime.utcnow(),
        updated=datetime.utcnow(),
        body="This is as designed. See the documentation.",
    )
    
    result = classifier.classify(comment)
    
    assert result.comment_type == CommentType.AS_DESIGNED
