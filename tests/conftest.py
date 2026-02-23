"""Pytest configuration and fixtures"""

import pytest
from datetime import datetime
from src.models.comment import Comment


@pytest.fixture
def sample_comment():
    """Sample comment for testing"""
    return Comment(
        comment_id="10000",
        issue_key="DEFECT-123",
        author="dev@company.com",
        author_role="Developer",
        created=datetime.utcnow(),
        updated=datetime.utcnow(),
        body="Cannot reproduce this on my machine. Need environment details.",
        is_internal=False,
    )


@pytest.fixture
def sample_issue_key():
    """Sample issue key"""
    return "DEFECT-123"
