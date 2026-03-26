"""End-to-end pipeline integration test — Jira → Collector → Classifier → Drafter."""

import os
import pytest
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(".env", override=True)

from src.integrations.jira import JiraClient
from src.agent.context_collector import ContextCollector
from src.agent.classifier import CommentClassifier
from src.agent.drafter import ResponseDrafter
from src.models.comment import Comment

HAVE_JIRA = bool(
    os.getenv("JIRA_BASE_URL")
    and os.getenv("JIRA_USERNAME")
    and os.getenv("JIRA_API_TOKEN")
)

pytestmark = pytest.mark.skipif(
    not HAVE_JIRA,
    reason="Jira credentials not configured in .env",
)

ISSUE_KEY = "IP-7"


@pytest.fixture(scope="module")
def jira():
    return JiraClient()


@pytest.fixture(scope="module")
def collector(jira):
    return ContextCollector(jira_client=jira)


@pytest.fixture(scope="module")
def classifier():
    return CommentClassifier()


@pytest.fixture(scope="module")
def drafter():
    return ResponseDrafter()


@pytest.fixture(scope="module")
def context_result(collector):
    """Collect context once for the whole module."""
    return collector.collect(ISSUE_KEY)


def _make_comment(issue_key: str, body: str) -> Comment:
    """Build a Comment model for classification / drafting."""
    return Comment(
        comment_id="live-test",
        issue_key=issue_key,
        author="integration-test",
        author_role="Tester",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body=body,
        is_internal=False,
    )


class TestContextCollection:
    """Collect live context from IP-7."""

    def test_collect_returns_result(self, context_result):
        assert context_result is not None
        assert context_result.issue_context is not None

    def test_context_has_summary(self, context_result):
        ctx = context_result.issue_context
        assert isinstance(ctx.summary, str)
        assert len(ctx.summary) > 0

    def test_context_has_issue_type(self, context_result):
        ctx = context_result.issue_context
        assert ctx.issue_type == "Bug"

    def test_context_has_status(self, context_result):
        ctx = context_result.issue_context
        assert isinstance(ctx.status, str)
        assert len(ctx.status) > 0


class TestClassification:
    """Classify a synthetic comment in the context of IP-7."""

    def test_classify_returns_result(self, classifier):
        comment = _make_comment(
            ISSUE_KEY,
            "Cannot reproduce this issue on the latest build. "
            "Please provide environment details.",
        )
        classification = classifier.classify(comment)
        assert classification is not None
        assert classification.comment_type is not None
        assert 0 <= classification.confidence <= 1

    def test_classify_bug_report(self, classifier):
        comment = _make_comment(
            ISSUE_KEY,
            "After NIC failover the VM workload stopped and sync took over 1 hour.",
        )
        classification = classifier.classify(comment)
        assert classification is not None


class TestDraftGeneration:
    """Draft a reply for IP-7."""

    def test_draft_reply(self, context_result, classifier, drafter):
        comment = _make_comment(
            ISSUE_KEY,
            "Cannot reproduce this issue on the latest build. "
            "Please provide environment details.",
        )
        classification = classifier.classify(comment)
        draft = drafter.draft(
            comment=comment,
            classification=classification,
            context=context_result,
        )
        assert draft is not None
        assert isinstance(draft.body, str)
        assert len(draft.body) > 0
        assert draft.issue_key == ISSUE_KEY

    def test_draft_has_confidence(self, context_result, classifier, drafter):
        comment = _make_comment(
            ISSUE_KEY,
            "This looks like a configuration issue with the NIC settings.",
        )
        classification = classifier.classify(comment)
        draft = drafter.draft(
            comment=comment,
            classification=classification,
            context=context_result,
        )
        assert 0 <= draft.confidence_score <= 1
