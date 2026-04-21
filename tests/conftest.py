"""Pytest configuration and fixtures"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from src.models.comment import Comment


class _DisabledLLMClient:
    """A stub LLM client that reports itself as disabled (no API key)."""
    enabled = False
    backend = "none"

    def complete(self, **kwargs):
        return None


@pytest.fixture
def disabled_llm():
    """Return a disabled LLM client for deterministic template-only tests."""
    return _DisabledLLMClient()


@pytest.fixture
def sample_comment():
    """Sample comment for testing"""
    return Comment(
        comment_id="10000",
        issue_key="DEFECT-123",
        author="dev@company.com",
        author_role="Developer",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body="Cannot reproduce this on my machine. Need environment details.",
        is_internal=False,
    )


@pytest.fixture
def sample_issue_key():
    """Sample issue key"""
    return "DEFECT-123"
