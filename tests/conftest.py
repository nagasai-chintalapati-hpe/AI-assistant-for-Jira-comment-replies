"""Pytest configuration and fixtures."""

import pytest
from datetime import datetime, timezone
from src.models.comment import Comment


@pytest.fixture
def sample_comment():
    """Sample comment for testing."""
    return Comment(
        comment_id="10000",
        issue_key="DEFECT-123",
        author="dev@company.com",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body="Cannot reproduce this on my machine. Need environment details.",
    )


@pytest.fixture
def sample_issue_key():
    """Sample issue key for testing."""
    return "DEFECT-123"

