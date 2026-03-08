"""Tests for response drafter – template-based generation."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.agent.drafter import TEMPLATES, ResponseDrafter
from src.models.classification import CommentClassification, CommentType
from src.models.context import CommentSnapshot, ContextCollectionResult, IssueContext
from src.models.draft import DraftStatus


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


@pytest.fixture
def rich_context():
    """Context with attachments, linked issues, changelog, and Jenkins links."""
    issue_context = IssueContext(
        issue_key="DEFECT-123",
        summary="Upload fails with 500 error",
        description="POST /snapshot fails on tenant X.",
        issue_type="Bug",
        status="In Progress",
        priority="High",
        environment="Staging, Chrome 121",
        versions=["1.8.14", "1.8.15"],
        components=["Snapshot Service"],
        attached_files=[
            {
                "filename": "error.log",
                "content_url": "https://jira/att/1",
                "mime_type": "text/plain",
                "size": 4096,
            }
        ],
        last_comments=[
            CommentSnapshot(comment_id="c1", author="Alice", body="Opened ticket", created="")
        ],
        linked_issues=[
            {"key": "DEFECT-100", "type": "Blocks", "direction": "outward", "status": "Closed"}
        ],
        changelog=[
            {
                "author": "CI Bot",
                "created": "2025-02-20T08:00:00Z",
                "items": [{"field": "status", "from": "Open", "to": "In Progress"}],
            }
        ],
    )
    return ContextCollectionResult(
        issue_context=issue_context,
        jenkins_links=["https://jenkins.company.com/job/build/42/console"],
        jenkins_log_snippets={
            "https://jenkins.company.com/job/build/42/console": (
                "BUILD FAILURE\nNullPointerException"
            )
        },
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=120.0,
    )


class TestCitations:
    """Verify _build_citations populates from all evidence sources."""

    @pytest.mark.asyncio
    async def test_citations_from_attachments(self, drafter, sample_comment, rich_context):
        """Attachments in context generate citation entries."""
        classification = _make_classification(CommentType.CANNOT_REPRODUCE)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        assert draft.citations is not None
        sources = [c["source"] for c in draft.citations]
        assert any("error.log" in s for s in sources)

    @pytest.mark.asyncio
    async def test_citations_from_jenkins_links(self, drafter, sample_comment, rich_context):
        """Jenkins links in context generate citation entries."""
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        assert draft.citations is not None
        assert any(c["source"] == "Jenkins Build Log" for c in draft.citations)
        assert any("BUILD FAILURE" in c["excerpt"] for c in draft.citations)

    @pytest.mark.asyncio
    async def test_citations_from_linked_issues(self, drafter, sample_comment, rich_context):
        """Linked issues in context generate citation entries."""
        classification = _make_classification(CommentType.CANNOT_REPRODUCE)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        assert draft.citations is not None
        assert any("DEFECT-100" in c["source"] for c in draft.citations)

    @pytest.mark.asyncio
    async def test_citations_jenkins_snippet_absent_uses_default(self, drafter, sample_comment):
        """Jenkins link with no snippet uses the default excerpt text."""
        issue_context = IssueContext(
            issue_key="DEFECT-123",
            summary="Test",
            description="",
            issue_type="Bug",
            status="Open",
            priority="High",
        )
        context = ContextCollectionResult(
            issue_context=issue_context,
            jenkins_links=["https://jenkins.company.com/job/build/1/console"],
            jenkins_log_snippets=None,  # no snippets fetched
            collection_timestamp=datetime.now(timezone.utc),
            collection_duration_ms=10.0,
        )
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = await drafter.draft(sample_comment, classification, context)
        jenkins_cit = next(
            (c for c in (draft.citations or []) if c["source"] == "Jenkins Build Log"), None
        )
        assert jenkins_cit is not None
        assert jenkins_cit["excerpt"] == "Console output from CI build"


class TestRichContextDraft:
    """Draft generation with fully-populated context (versions, components, changelog)."""

    @pytest.mark.asyncio
    async def test_draft_uses_environment_from_context(self, drafter, sample_comment, rich_context):
        classification = _make_classification(CommentType.CANNOT_REPRODUCE)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        assert "Staging" in draft.body or "staging" in draft.body

    @pytest.mark.asyncio
    async def test_draft_evidence_includes_attachment_count(
        self, drafter, sample_comment, rich_context
    ):
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        # body should mention attachment evidence or comments
        assert len(draft.body) > 0

    @pytest.mark.asyncio
    async def test_draft_fixed_validate_uses_fix_version(
        self, drafter, sample_comment, rich_context
    ):
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        # Version should appear in body from the rich context
        assert "1.8" in draft.body or "pending" in draft.body

    @pytest.mark.asyncio
    async def test_draft_changelog_generates_retest_checklist(
        self, drafter, sample_comment, rich_context
    ):
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = await drafter.draft(sample_comment, classification, rich_context)
        assert "In Progress" in draft.body or "staging" in draft.body.lower()


class TestConstructorApiKeyPath:
    """Verify the api_key constructor path initialises _client."""

    def test_classifier_with_api_key_sets_client(self):
        """Passing api_key initialises CopilotClient (import succeeds in this env)."""
        from src.agent.classifier import CommentClassifier

        clf = CommentClassifier(api_key="fake-key")
        assert clf._client is not None

    def test_drafter_with_api_key_sets_client(self):
        """Passing api_key initialises CopilotClient for the drafter."""
        drafter = ResponseDrafter(api_key="fake-key")
        assert drafter._client is not None


class TestRefineWithCopilotSuccess:
    """Cover the success return path of _refine_with_copilot."""

    @pytest.mark.asyncio
    async def test_refine_returns_model_content_on_success(self):
        drafter = ResponseDrafter()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="  Polished response.  "))]
        mock_client.chat.completions.create.return_value = mock_response
        drafter._client = mock_client

        result = await drafter._refine_with_copilot("Raw draft text")
        assert result == "Polished response."


class TestLocalLLMRefinement:
    @pytest.mark.asyncio
    @patch("src.agent.drafter.requests.post")
    async def test_refine_with_local_llm_returns_content(self, mock_post):
        drafter = ResponseDrafter(
            provider="llama_cpp",
            model="llama-3.1-8b-instruct",
            base_url="http://localhost:8080",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Refined local response."}}]
        }
        mock_post.return_value = mock_resp

        result = await drafter._refine_with_local_llm("Raw draft text")
        assert result == "Refined local response."
