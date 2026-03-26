"""Tests for response drafter – template-based generation."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.drafter import ResponseDrafter
from src.models.classification import CommentClassification, CommentType
from src.models.context import CommentSnapshot, ContextCollectionResult, IssueContext
from src.models.draft import DraftStatus
from src.models.rag import RAGSnippet, LogEntry
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
        mock_client.create_session = AsyncMock(side_effect=RuntimeError("boom"))
        drafter._client = mock_client

        refined = await drafter._refine_with_copilot("Hello")

        assert refined is None


class TestAllTypesProduceDraft:
    """Verify every CommentType produces a non-empty draft."""

    @pytest.mark.asyncio
    async def test_all_types_generate_draft(self, drafter, sample_comment, sample_context):
        """Every classification type should produce a non-empty draft body."""
        for ctype in CommentType:
            classification = _make_classification(ctype)
            draft = await drafter.draft(sample_comment, classification, sample_context)
            assert draft.body, f"Empty draft body for {ctype}"
            assert draft.draft_id.startswith("draft_"), f"Bad draft_id for {ctype}"


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

    def test_all_types_have_templates(self):
        for ctype in CommentType:
            assert ctype in TEMPLATES, f"Missing template for {ctype}"


# ---- Evidence-enriched context helpers --------------------------------- #

def _make_enriched_context(
    log_entries=None, testrail_results=None, build_metadata=None,
    rag_snippets=None, jenkins_links=None,
):
    """Build a ContextCollectionResult with Phase 3 fields populated."""
    issue_context = IssueContext(
        issue_key="DEFECT-300",
        summary="API timeout on staging",
        description="POST /api/data returns 504",
        issue_type="Bug",
        status="In Progress",
        priority="High",
        environment="Staging, k8s v1.28",
        versions=["2.3.0"],
        components=["API Gateway"],
    )
    return ContextCollectionResult(
        issue_context=issue_context,
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=200.0,
        log_entries=log_entries,
        testrail_results=testrail_results,
        build_metadata=build_metadata,
        rag_snippets=rag_snippets,
        jenkins_links=jenkins_links,
    )


class TestDrafterWithLogEntries:
    def test_log_entries_in_evidence(self, drafter, sample_comment):
        entries = [
            LogEntry(
                source="jenkins",
                message="ERROR: Connection timeout to DB at 10:30:12",
                level="ERROR",
                correlation_id="req-abc123",
            ),
        ]
        ctx = _make_enriched_context(log_entries=entries)
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = drafter.draft(sample_comment, classification, ctx)

        assert draft.evidence_used is not None
        assert any("Log" in e for e in draft.evidence_used)
        assert any("Log" in c["source"] for c in draft.citations)

    def test_log_preview_in_body(self, drafter, sample_comment):
        entries = [
            LogEntry(
                source="jenkins",
                message="java.lang.NullPointerException at MainService.run",
                level="ERROR",
            ),
        ]
        ctx = _make_enriched_context(log_entries=entries)
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = drafter.draft(sample_comment, classification, ctx)

        # The evidence section in body should mention the log
        assert "Log" in draft.body or "jenkins" in draft.body.lower()


class TestDrafterWithTestRailResults:
    def test_testrail_in_evidence(self, drafter, sample_comment):
        tr_results = [
            {
                "name": "Sprint 42 Regression",
                "pass_rate": 94.5,
                "failed": 3,
                "url": "https://testrail.co/runs/100",
                "failed_tests": [
                    {"title": "Upload test"},
                    {"title": "Login test"},
                ],
            },
        ]
        ctx = _make_enriched_context(testrail_results=tr_results)
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = drafter.draft(sample_comment, classification, ctx)

        assert draft.evidence_used is not None
        assert any("TestRail" in e for e in draft.evidence_used)
        assert any("TestRail" in c["source"] for c in draft.citations)

    def test_testrail_in_body(self, drafter, sample_comment):
        tr_results = [
            {"name": "Smoke Tests", "pass_rate": 100, "failed": 0},
        ]
        ctx = _make_enriched_context(testrail_results=tr_results)
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = drafter.draft(sample_comment, classification, ctx)

        assert "TestRail" in draft.body


class TestDrafterWithBuildMetadata:
    def test_build_version_from_metadata(self, drafter, sample_comment):
        bm = {"commit": "abc1234", "version": "2.5.0-rc1"}
        ctx = _make_enriched_context(build_metadata=bm)
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, ctx)

        # Build version should come from metadata, not issue versions
        assert "2.5.0-rc1" in draft.body

    def test_build_metadata_in_evidence_used(self, drafter, sample_comment):
        bm = {"commit": "abc1234", "version": "2.5.0"}
        ctx = _make_enriched_context(build_metadata=bm)
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, ctx)

        assert draft.evidence_used is not None
        assert any("Build" in e for e in draft.evidence_used)


class TestDrafterRetestChecklist:
    def test_retest_checklist_includes_failed_tests(self, drafter, sample_comment):
        tr_results = [
            {
                "name": "Regression Suite",
                "pass_rate": 90,
                "failed": 2,
                "failed_tests": [
                    {"title": "Upload large file"},
                    {"title": "Delete expired session"},
                ],
            },
        ]
        bm = {"version": "2.5.0"}
        ctx = _make_enriched_context(testrail_results=tr_results, build_metadata=bm)
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, ctx)

        assert "Upload large file" in draft.body
        assert "2.5.0" in draft.body

    def test_retest_checklist_default_when_no_testrail(self, drafter, sample_comment):
        ctx = _make_enriched_context()
        classification = _make_classification(CommentType.FIXED_VALIDATE)
        draft = drafter.draft(sample_comment, classification, ctx)

        assert "Verify the reported scenario" in draft.body


class TestDrafterCombinedSources:
    def test_all_sources_combined(self, drafter, sample_comment):
        """Log, TestRail, build metadata, Jenkins, and RAG all present."""
        log_entries = [
            LogEntry(source="jenkins", message="Build FAILED", level="ERROR"),
        ]
        tr_results = [
            {"name": "Run 50", "pass_rate": 80, "failed": 5},
        ]
        bm = {"commit": "def5678", "version": "3.0.0"}
        jenkins_links = ["https://jenkins.co/job/main/50/consoleFull"]
        rag_snippets = [
            RAGSnippet(
                chunk_id="c1",
                source_type="confluence",
                source_title="Troubleshooting Guide",
                content="Check the API gateway logs for timeout errors",
                relevance_score=0.85,
            ),
        ]
        ctx = _make_enriched_context(
            log_entries=log_entries,
            testrail_results=tr_results,
            build_metadata=bm,
            jenkins_links=jenkins_links,
            rag_snippets=rag_snippets,
        )
        classification = _make_classification(CommentType.NEED_MORE_INFO)
        draft = drafter.draft(sample_comment, classification, ctx)

        # Should have evidence from all sources
        assert draft.evidence_used is not None
        sources = " ".join(draft.evidence_used)
        assert "Jenkins" in sources
        assert "Log" in sources
        assert "TestRail" in sources
        assert "Build" in sources
        assert "Confluence" in sources or "Troubleshooting" in sources
