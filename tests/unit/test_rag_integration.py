"""Tests for RAG integration with context collector and drafter."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from src.models.rag import RAGSnippet, RAGResult, DocumentChunk
from src.models.context import ContextCollectionResult, IssueContext
from src.models.classification import CommentClassification, CommentType
from src.models.comment import Comment
from src.agent.drafter import ResponseDrafter
from src.rag.engine import RAGEngine


# Fixtures

@pytest.fixture
def sample_rag_snippets():
    return [
        RAGSnippet(
            chunk_id="c1",
            source_type="confluence",
            source_title="Login Troubleshooting Guide",
            source_url="https://wiki.example.com/pages/123",
            content="When users cannot log in, verify the SSO configuration in the admin panel.",
            relevance_score=0.85,
        ),
        RAGSnippet(
            chunk_id="c2",
            source_type="pdf",
            source_title="Release Notes v2.3",
            content="Fixed authentication timeout issue affecting OAuth tokens.",
            relevance_score=0.62,
        ),
    ]


@pytest.fixture
def context_with_rag(sample_rag_snippets):
    return ContextCollectionResult(
        issue_context=IssueContext(
            issue_key="DEFECT-100",
            summary="Login fails intermittently",
            description="Users report login failures",
            issue_type="Bug",
            status="Open",
            priority="High",
            environment="Production",
            versions=["2.3.0"],
            components=["auth"],
        ),
        jenkins_links=["https://jenkins.example.com/job/build/42/console"],
        rag_snippets=sample_rag_snippets,
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=150.0,
    )


@pytest.fixture
def context_without_rag():
    return ContextCollectionResult(
        issue_context=IssueContext(
            issue_key="DEFECT-200",
            summary="Button misaligned",
            description="UI issue",
            issue_type="Bug",
            status="Open",
            priority="Low",
        ),
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=50.0,
    )


@pytest.fixture
def comment():
    return Comment(
        comment_id="c-123",
        issue_key="DEFECT-100",
        author="dev@example.com",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body="Cannot reproduce this login issue on staging.",
    )


@pytest.fixture
def classification():
    return CommentClassification(
        comment_id="c-123",
        comment_type=CommentType.CANNOT_REPRODUCE,
        confidence=0.9,
        reasoning="Developer states they cannot reproduce",
    )


@pytest.fixture
def real_rag_engine(tmp_path):
    """Real RAGEngine backed by ChromaDB in a temp directory."""
    chroma_dir = str(tmp_path / "chroma")
    with patch("src.rag.engine.settings") as mock_settings:
        mock_settings.rag.chroma_persist_dir = chroma_dir
        mock_settings.rag.embedding_model = "all-MiniLM-L6-v2"
        mock_settings.rag.top_k = 5

        eng = RAGEngine(
            persist_dir=chroma_dir,
            collection_name="test_integration",
        )
        # Pre-populate with some relevant documents
        eng.add_chunks([
            DocumentChunk(
                chunk_id="doc-auth-0",
                source_type="confluence",
                source_title="Auth Runbook",
                source_url="https://wiki.example.com/auth",
                content="When users cannot log in, check the SSO configuration in the admin panel. Verify IdP certificate expiry.",
            ),
            DocumentChunk(
                chunk_id="doc-deploy-0",
                source_type="pdf",
                source_title="Deployment Guide",
                content="To deploy version 2.4, run the CI pipeline on the release branch and verify integration tests.",
            ),
        ])
        yield eng


# Tests — Drafter with RAG evidence

class TestDrafterRAGIntegration:
    def test_evidence_formatting_includes_rag_snippets(self, context_with_rag):
        evidence = ResponseDrafter._format_existing_evidence(context_with_rag)
        assert "Login Troubleshooting Guide" in evidence
        assert "85%" in evidence
        assert "Release Notes v2.3" in evidence
        assert "Jenkins log" in evidence

    def test_evidence_formatting_without_rag(self, context_without_rag):
        evidence = ResponseDrafter._format_existing_evidence(context_without_rag)
        assert "none collected yet" in evidence

    def test_citations_include_rag_sources(self, context_with_rag):
        citations = ResponseDrafter._build_citations(context_with_rag)
        assert len(citations) == 3
        sources = [c["source"] for c in citations]
        assert "Jenkins" in sources
        assert "Login Troubleshooting Guide" in sources
        assert "Release Notes v2.3" in sources

    def test_citation_includes_url_when_available(self, context_with_rag):
        citations = ResponseDrafter._build_citations(context_with_rag)
        confluence_citation = [c for c in citations if c["source"] == "Login Troubleshooting Guide"][0]
        assert confluence_citation["url"] == "https://wiki.example.com/pages/123"

    def test_citation_no_url_for_pdf(self, context_with_rag):
        citations = ResponseDrafter._build_citations(context_with_rag)
        pdf_citation = [c for c in citations if c["source"] == "Release Notes v2.3"][0]
        assert "url" not in pdf_citation

    def test_citations_empty_without_rag(self, context_without_rag):
        citations = ResponseDrafter._build_citations(context_without_rag)
        assert citations == []

    def test_evidence_used_list(self, context_with_rag):
        evidence = ResponseDrafter._build_evidence_used(context_with_rag)
        assert len(evidence) == 3  # 1 Jenkins + 2 RAG
        assert any("Jenkins" in e for e in evidence)
        assert any("Confluence: Login Troubleshooting Guide" in e for e in evidence)
        assert any("Pdf: Release Notes v2.3" in e for e in evidence)

    def test_evidence_used_relevance_threshold(self, context_with_rag):
        """Both snippets >= 0.5 so both should show relevance."""
        evidence = ResponseDrafter._build_evidence_used(context_with_rag)
        relevance_items = [e for e in evidence if "match)" in e or "relevance:" in e]
        assert len(relevance_items) == 2

    def test_evidence_used_empty_without_rag(self, context_without_rag):
        evidence = ResponseDrafter._build_evidence_used(context_without_rag)
        assert evidence == []

    def test_draft_populates_evidence_fields(self, comment, classification, context_with_rag):
        """Full draft generation should populate evidence_used and citations."""
        drafter = ResponseDrafter()
        draft = drafter.draft(comment, classification, context_with_rag)

        assert draft.evidence_used is not None
        assert len(draft.evidence_used) == 3
        assert draft.citations is not None
        assert len(draft.citations) == 3
        assert draft.classification_type == "cannot_reproduce"
        assert draft.classification_reasoning == "Developer states they cannot reproduce"

    def test_draft_without_rag_has_no_evidence(self, classification, context_without_rag):
        drafter = ResponseDrafter()
        alt_comment = Comment(
            comment_id="c-200",
            issue_key="DEFECT-200",
            author="dev@example.com",
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
            body="Works on my machine.",
        )
        draft = drafter.draft(alt_comment, classification, context_without_rag)
        assert draft.evidence_used is None
        assert draft.citations == []


# Tests — Context Collector with real RAG

class TestContextCollectorRAG:
    def _make_mock_jira(self, summary="Login timeout", description="Auth service fails"):
        """Build a mock Jira client (external API — mock is appropriate)."""
        mock_jira = MagicMock()
        mock_jira.get_issue.return_value = {
            "fields": {
                "summary": summary,
                "description": description,
                "issuetype": {"name": "Bug"},
                "status": {"name": "Open"},
                "priority": {"name": "High"},
                "comment": {"comments": []},
            }
        }
        mock_jira.get_last_comments.return_value = []
        mock_jira.get_attachments.return_value = []
        mock_jira.get_linked_issues.return_value = []
        mock_jira.get_changelog.return_value = []
        mock_jira.detect_jenkins_links.return_value = []
        return mock_jira

    def test_collector_queries_rag_when_engine_provided(self, real_rag_engine):
        """ContextCollector should populate rag_snippets from real ChromaDB."""
        from src.agent.context_collector import ContextCollector

        mock_jira = self._make_mock_jira(
            summary="Login timeout",
            description="Auth service fails with SSO",
        )

        collector = ContextCollector(jira_client=mock_jira, rag_engine=real_rag_engine)
        result = collector.collect("DEFECT-1")

        assert result.rag_snippets is not None
        assert len(result.rag_snippets) >= 1
        # The auth runbook should be returned since the issue is about login/auth
        titles = [s.source_title for s in result.rag_snippets]
        assert any("Auth" in t for t in titles)

    def test_collector_works_without_rag_engine(self):
        """ContextCollector should work without RAG (backward compatible)."""
        from src.agent.context_collector import ContextCollector

        mock_jira = self._make_mock_jira(summary="Bug", description="desc")
        collector = ContextCollector(jira_client=mock_jira)  # no rag_engine
        result = collector.collect("DEFECT-2")

        assert result.rag_snippets is None

    def test_collector_handles_rag_failure_gracefully(self, real_rag_engine):
        """If RAG engine throws, collector should continue without snippets."""
        from src.agent.context_collector import ContextCollector

        mock_jira = self._make_mock_jira(summary="Bug", description="desc")

        # Sabotage the engine's query method to simulate a failure
        original_query = real_rag_engine.query
        real_rag_engine.query = MagicMock(side_effect=RuntimeError("ChromaDB unavailable"))

        collector = ContextCollector(jira_client=mock_jira, rag_engine=real_rag_engine)
        result = collector.collect("DEFECT-3")

        assert result.rag_snippets is None
        assert result.issue_context.issue_key == "DEFECT-3"

        # Restore for cleanup
        real_rag_engine.query = original_query
