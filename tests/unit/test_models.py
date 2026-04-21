"""Tests for extended classification and new models."""

import pytest
from datetime import datetime, timezone

from src.models.classification import CommentType, CommentClassification
from src.models.comment import Comment
from src.models.rag import RAGSnippet, RAGResult, LogEntry, DocumentChunk
from src.models.context import ContextCollectionResult, IssueContext
from src.models.draft import Draft, DraftStatus
from src.agent.classifier import CommentClassifier
from src.agent.drafter import ResponseDrafter, TEMPLATES


# Helper

def _make_comment(body: str) -> Comment:
    return Comment(
        comment_id="20000",
        issue_key="DEFECT-200",
        author="dev@company.com",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        body=body,
    )


def _make_context(issue_key: str = "DEFECT-200") -> ContextCollectionResult:
    return ContextCollectionResult(
        issue_context=IssueContext(
            issue_key=issue_key,
            summary="Login fails intermittently",
            description="Users report login failures",
            issue_type="Bug",
            status="Open",
            priority="High",
            environment="Staging",
            versions=["1.8.14"],
            components=["Auth"],
            linked_issues=[
                {"key": "DEFECT-150", "type": "Blocks", "direction": "inward", "status": "Open"}
            ],
        ),
        collection_timestamp=datetime.now(timezone.utc),
        collection_duration_ms=50.0,
    )


# New CommentType values exist

class TestNewCommentTypes:
    def test_duplicate_fixed_enum_exists(self):
        assert CommentType.DUPLICATE_FIXED.value == "duplicate_fixed"

    def test_blocked_waiting_enum_exists(self):
        assert CommentType.BLOCKED_WAITING.value == "blocked_waiting"

    def test_config_issue_enum_exists(self):
        assert CommentType.CONFIG_ISSUE.value == "config_issue"

    def test_all_types_count(self):
        """There should be 8 classification types total."""
        assert len(CommentType) == 8


# Keyword classification for new buckets

class TestNewKeywordClassification:
    @pytest.fixture
    def classifier(self, disabled_llm):
        return CommentClassifier(llm_client=disabled_llm) 

    def test_duplicate_keyword(self, classifier):
        comment = _make_comment("This is a duplicate of DEFECT-50, already reported.")
        result = classifier.classify(comment)
        assert result.comment_type == CommentType.DUPLICATE_FIXED

    def test_known_issue_keyword(self, classifier):
        comment = _make_comment("This is a known issue. See the release notes.")
        result = classifier.classify(comment)
        assert result.comment_type == CommentType.DUPLICATE_FIXED

    def test_blocked_by_keyword(self, classifier):
        comment = _make_comment("We're blocked by the database migration in INFRA-22.")
        result = classifier.classify(comment)
        assert result.comment_type == CommentType.BLOCKED_WAITING

    def test_waiting_for_keyword(self, classifier):
        comment = _make_comment("Waiting for the API team to deploy the fix.")
        result = classifier.classify(comment)
        assert result.comment_type == CommentType.BLOCKED_WAITING

    def test_config_issue_keyword(self, classifier):
        comment = _make_comment("This is a configuration issue, not a code bug.")
        result = classifier.classify(comment)
        assert result.comment_type == CommentType.CONFIG_ISSUE

    def test_not_a_bug_keyword(self, classifier):
        comment = _make_comment("Investigated — not a bug, it's a setup issue.")
        result = classifier.classify(comment)
        assert result.comment_type == CommentType.CONFIG_ISSUE

    def test_suggested_questions_for_duplicate(self, classifier):
        comment = _make_comment("This is a duplicate of DEFECT-50.")
        result = classifier.classify(comment)
        assert result.suggested_questions is not None
        assert len(result.suggested_questions) > 0

    def test_suggested_questions_for_blocked(self, classifier):
        comment = _make_comment("Blocked by INFRA-22.")
        result = classifier.classify(comment)
        assert result.suggested_questions is not None

    def test_suggested_questions_for_config(self, classifier):
        comment = _make_comment("This is a configuration issue.")
        result = classifier.classify(comment)
        assert result.suggested_questions is not None


# New templates exist

class TestNewTemplates:
    def test_duplicate_fixed_template_exists(self):
        assert CommentType.DUPLICATE_FIXED in TEMPLATES

    def test_blocked_waiting_template_exists(self):
        assert CommentType.BLOCKED_WAITING in TEMPLATES

    def test_config_issue_template_exists(self):
        assert CommentType.CONFIG_ISSUE in TEMPLATES

    def test_all_types_have_templates(self):
        for ctype in CommentType:
            assert ctype in TEMPLATES, f"Missing template for {ctype}"


# Draft generation for new buckets

class TestNewDraftGeneration:
    @pytest.fixture
    def drafter(self):
        return ResponseDrafter()  # template-only mode

    def test_draft_for_duplicate_fixed(self, drafter):
        comment = _make_comment("This is a duplicate of DEFECT-50.")
        classification = CommentClassification(
            comment_id="20000",
            comment_type=CommentType.DUPLICATE_FIXED,
            confidence=0.85,
            reasoning="Duplicate",
        )
        context = _make_context()
        draft = drafter.draft(comment, classification, context)
        assert "duplicate" in draft.body.lower()
        assert draft.confidence_score == 0.85
        assert "duplicate" in draft.suggested_labels

    def test_draft_for_blocked_waiting(self, drafter):
        comment = _make_comment("Blocked by INFRA-22.")
        classification = CommentClassification(
            comment_id="20000",
            comment_type=CommentType.BLOCKED_WAITING,
            confidence=0.85,
            reasoning="Blocked",
        )
        context = _make_context()
        draft = drafter.draft(comment, classification, context)
        assert "blocked" in draft.body.lower() or "waiting" in draft.body.lower()
        assert "blocked" in draft.suggested_labels

    def test_draft_for_config_issue(self, drafter):
        comment = _make_comment("This is a configuration issue.")
        classification = CommentClassification(
            comment_id="20000",
            comment_type=CommentType.CONFIG_ISSUE,
            confidence=0.85,
            reasoning="Config issue",
        )
        context = _make_context()
        draft = drafter.draft(comment, classification, context)
        assert "configuration" in draft.body.lower() or "setup" in draft.body.lower()
        assert "config-issue" in draft.suggested_labels

    def test_suggested_actions_for_duplicate(self, drafter):
        comment = _make_comment("Duplicate.")
        classification = CommentClassification(
            comment_id="20000",
            comment_type=CommentType.DUPLICATE_FIXED,
            confidence=0.85,
            reasoning="Dup",
        )
        context = _make_context()
        draft = drafter.draft(comment, classification, context)
        action_values = [a["value"] for a in draft.suggested_actions]
        assert "Closed" in action_values
        assert "duplicate" in action_values

    def test_suggested_actions_for_blocked(self, drafter):
        comment = _make_comment("Blocked.")
        classification = CommentClassification(
            comment_id="20000",
            comment_type=CommentType.BLOCKED_WAITING,
            confidence=0.85,
            reasoning="Blocked",
        )
        context = _make_context()
        draft = drafter.draft(comment, classification, context)
        action_values = [a["value"] for a in draft.suggested_actions]
        assert "Blocked" in action_values


# RAG model tests

class TestRAGModels:
    def test_rag_snippet_creation(self):
        snippet = RAGSnippet(
            chunk_id="chunk_001",
            source_type="confluence",
            source_title="Login Troubleshooting",
            source_url="https://wiki.example.com/login",
            content="Check feature flag auth_v2 is enabled...",
            relevance_score=0.92,
            metadata={"component": "Auth", "version": "1.8"},
        )
        assert snippet.relevance_score == 0.92
        assert snippet.source_type == "confluence"

    def test_rag_result_creation(self):
        result = RAGResult(
            query="login failure staging",
            snippets=[],
            total_chunks_searched=1000,
            retrieval_duration_ms=45.2,
        )
        assert result.total_chunks_searched == 1000
        assert result.snippets == []

    def test_log_entry_creation(self):
        entry = LogEntry(
            source="jenkins",
            correlation_id="corr-12345",
            timestamp="2026-02-06T14:12:00Z",
            level="ERROR",
            message="SnapshotLockTimeout in SnapshotManager.acquire()",
            context={"build_id": "42", "env": "staging"},
        )
        assert entry.level == "ERROR"
        assert entry.correlation_id == "corr-12345"

    def test_document_chunk_creation(self):
        chunk = DocumentChunk(
            chunk_id="doc_001",
            source_type="pdf",
            source_title="API Reference v2",
            content="The POST /snapshot endpoint requires...",
            metadata={"component": "Snapshot", "version": "2.0"},
        )
        assert chunk.source_type == "pdf"


# Context model with new fields

class TestContextWithRAGFields:
    def test_context_result_with_rag_snippets(self):
        snippet = RAGSnippet(
            chunk_id="c1",
            source_type="confluence",
            source_title="Test",
            content="content",
            relevance_score=0.8,
        )
        result = ContextCollectionResult(
            issue_context=IssueContext(
                issue_key="DEFECT-1",
                summary="test",
                description="desc",
                issue_type="Bug",
                status="Open",
                priority="High",
            ),
            rag_snippets=[snippet],
            collection_timestamp=datetime.now(timezone.utc),
            collection_duration_ms=10.0,
        )
        assert len(result.rag_snippets) == 1

    def test_context_result_with_log_entries(self):
        entry = LogEntry(source="jenkins", message="Error occurred")
        result = ContextCollectionResult(
            issue_context=IssueContext(
                issue_key="DEFECT-1",
                summary="test",
                description="desc",
                issue_type="Bug",
                status="Open",
                priority="High",
            ),
            log_entries=[entry],
            collection_timestamp=datetime.now(timezone.utc),
            collection_duration_ms=10.0,
        )
        assert len(result.log_entries) == 1

    def test_context_backward_compatible(self):
        """Original style context still works (no new fields)."""
        result = ContextCollectionResult(
            issue_context=IssueContext(
                issue_key="DEFECT-1",
                summary="test",
                description="desc",
                issue_type="Bug",
                status="Open",
                priority="High",
            ),
            collection_timestamp=datetime.now(timezone.utc),
            collection_duration_ms=10.0,
        )
        assert result.rag_snippets is None
        assert result.log_entries is None
        assert result.testrail_results is None
        assert result.build_metadata is None


# Draft model with new fields

class TestDraftWithNewFields:
    def test_draft_with_evidence_used(self):
        draft = Draft(
            draft_id="d1",
            issue_key="DEFECT-1",
            in_reply_to_comment_id="c1",
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body="Test",
            confidence_score=0.8,
            evidence_used=["Jenkins log #42", "Confluence: Login Troubleshooting"],
            missing_info=["Correlation ID", "Browser version"],
            classification_type="cannot_reproduce",
            classification_reasoning="Dev cannot reproduce",
        )
        assert len(draft.evidence_used) == 2
        assert len(draft.missing_info) == 2
        assert draft.classification_type == "cannot_reproduce"

    def test_draft_backward_compatible(self):
        """Original Draft still works (no new fields)."""
        draft = Draft(
            draft_id="d1",
            issue_key="DEFECT-1",
            in_reply_to_comment_id="c1",
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body="Test",
            confidence_score=0.8,
        )
        assert draft.evidence_used is None
        assert draft.missing_info is None
        assert draft.classification_type is None
