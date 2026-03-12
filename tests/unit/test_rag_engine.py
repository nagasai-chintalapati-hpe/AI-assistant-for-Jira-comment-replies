"""Tests for the RAG engine — ChromaDB-backed semantic retrieval.

Uses a real ChromaDB PersistentClient in a temp directory with real
sentence-transformer embeddings.  No mocks for local dependencies.
"""

import pytest
from unittest.mock import patch

from src.models.rag import DocumentChunk, RAGResult
from src.rag.engine import RAGEngine


# Fixtures

@pytest.fixture
def engine(tmp_path):
    """Create a real RAGEngine backed by ChromaDB in a temp directory."""
    chroma_dir = str(tmp_path / "chroma")
    with patch("src.rag.engine.settings") as mock_settings:
        mock_settings.rag.chroma_persist_dir = chroma_dir
        mock_settings.rag.embedding_model = "all-MiniLM-L6-v2"
        mock_settings.rag.top_k = 5

        eng = RAGEngine(
            persist_dir=chroma_dir,
            collection_name="test_collection",
        )
        yield eng


@pytest.fixture
def sample_chunks():
    """A handful of varied DocumentChunks for testing."""
    return [
        DocumentChunk(
            chunk_id="chunk-auth-0",
            source_type="confluence",
            source_title="Auth Runbook",
            source_url="https://wiki.example.com/auth",
            content="When users cannot log in, verify the SSO configuration in the admin panel. Check IdP metadata and certificate expiry.",
        ),
        DocumentChunk(
            chunk_id="chunk-auth-1",
            source_type="confluence",
            source_title="Auth Runbook",
            source_url="https://wiki.example.com/auth",
            content="OAuth token refresh failures usually indicate clock skew between the application server and the identity provider.",
        ),
        DocumentChunk(
            chunk_id="chunk-deploy-0",
            source_type="pdf",
            source_title="Deployment Guide",
            content="To deploy version 2.4 run the CI pipeline on the release branch. Ensure all integration tests pass before merging.",
        ),
        DocumentChunk(
            chunk_id="chunk-release-0",
            source_type="pdf",
            source_title="Release Notes v2.3",
            content="Fixed authentication timeout issue affecting OAuth tokens. Increased default token TTL to 3600 seconds.",
            metadata={"version": "2.3"},
        ),
    ]


# Tests — add_chunks

class TestRAGEngineAddChunks:
    def test_add_empty_list(self, engine):
        result = engine.add_chunks([])
        assert result == 0
        assert engine.collection_count == 0

    def test_add_single_chunk(self, engine):
        chunk = DocumentChunk(
            chunk_id="c1",
            source_type="pdf",
            source_title="test.pdf",
            content="Hello world — this is a test document.",
        )
        result = engine.add_chunks([chunk])
        assert result == 1
        assert engine.collection_count == 1

    def test_add_multiple_chunks(self, engine, sample_chunks):
        result = engine.add_chunks(sample_chunks)
        assert result == 4
        assert engine.collection_count == 4

    def test_upsert_overwrites_existing_id(self, engine):
        chunk_v1 = DocumentChunk(
            chunk_id="c1", source_type="pdf",
            source_title="doc.pdf", content="Original content",
        )
        chunk_v2 = DocumentChunk(
            chunk_id="c1", source_type="pdf",
            source_title="doc.pdf", content="Updated content",
        )
        engine.add_chunks([chunk_v1])
        engine.add_chunks([chunk_v2])
        assert engine.collection_count == 1  # same ID, no duplicate

    def test_add_chunk_with_metadata(self, engine):
        chunk = DocumentChunk(
            chunk_id="c1",
            source_type="runbook",
            source_title="Runbook",
            source_url="https://example.com/runbook",
            content="Troubleshooting steps for authentication failures.",
            metadata={"component": "auth", "version": "2.1"},
        )
        engine.add_chunks([chunk])
        assert engine.collection_count == 1


# Tests — query

class TestRAGEngineQuery:
    def test_query_returns_rag_result(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        result = engine.query("SSO login failure")

        assert isinstance(result, RAGResult)
        assert result.query == "SSO login failure"
        assert len(result.snippets) > 0
        assert result.total_chunks_searched == 4
        assert result.retrieval_duration_ms >= 0

    def test_query_relevance_ranking(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        result = engine.query("OAuth token refresh error")

        # The auth/release chunks should rank higher than deployment guide
        assert len(result.snippets) >= 2
        top_sources = [s.source_title for s in result.snippets[:2]]
        assert any("Auth" in t or "Release" in t for t in top_sources)

    def test_query_relevance_scores_between_0_and_1(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        result = engine.query("login authentication SSO")

        for snippet in result.snippets:
            assert 0.0 <= snippet.relevance_score <= 1.0

    def test_query_with_source_type_filter(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        result = engine.query("authentication", source_type="confluence")

        for snippet in result.snippets:
            assert snippet.source_type == "confluence"

    def test_query_with_custom_top_k(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        result = engine.query("auth", top_k=2)

        assert len(result.snippets) <= 2

    def test_query_empty_collection(self, engine):
        result = engine.query("anything at all")
        assert result.snippets == []
        assert result.total_chunks_searched == 0

    def test_query_snippet_metadata_separation(self, engine):
        chunk = DocumentChunk(
            chunk_id="c1",
            source_type="pdf",
            source_title="doc.pdf",
            source_url="https://example.com/doc",
            content="Deployment instructions for version 2.0 release.",
            metadata={"component": "deploy", "version": "2.0"},
        )
        engine.add_chunks([chunk])
        result = engine.query("deployment instructions")

        snippet = result.snippets[0]
        assert snippet.source_type == "pdf"
        assert snippet.source_title == "doc.pdf"
        # Extra metadata keys should be in snippet.metadata
        assert snippet.metadata is not None
        assert "component" in snippet.metadata


# Tests — delete

class TestRAGEngineDelete:
    def test_delete_by_source_found(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        assert engine.collection_count == 4

        count = engine.delete_by_source("Auth Runbook")
        assert count == 2
        assert engine.collection_count == 2

    def test_delete_by_source_not_found(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        count = engine.delete_by_source("Nonexistent Document")
        assert count == 0
        assert engine.collection_count == 4

    def test_delete_by_id_found(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        assert engine.delete_by_id("chunk-deploy-0") is True
        assert engine.collection_count == 3

    def test_delete_by_id_not_found(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        assert engine.delete_by_id("nonexistent-id") is False
        assert engine.collection_count == 4


# Tests — stats

class TestRAGEngineStats:
    def test_stats_empty_collection(self, engine):
        stats = engine.stats()
        assert stats["total_chunks"] == 0
        assert stats["sources"] == {}
        assert stats["collection_name"] == "test_collection"

    def test_stats_with_data(self, engine, sample_chunks):
        engine.add_chunks(sample_chunks)
        stats = engine.stats()

        assert stats["total_chunks"] == 4
        assert stats["sources"]["confluence"] == 2
        assert stats["sources"]["pdf"] == 2


# Tests — helpers

class TestRAGEngineHelpers:
    def test_generate_chunk_id_deterministic(self):
        id1 = RAGEngine.generate_chunk_id("doc.pdf", 0)
        id2 = RAGEngine.generate_chunk_id("doc.pdf", 0)
        assert id1 == id2
        assert len(id1) == 16

    def test_generate_chunk_id_unique_per_index(self):
        id1 = RAGEngine.generate_chunk_id("doc.pdf", 0)
        id2 = RAGEngine.generate_chunk_id("doc.pdf", 1)
        assert id1 != id2

    def test_generate_chunk_id_unique_per_source(self):
        id1 = RAGEngine.generate_chunk_id("doc_a.pdf", 0)
        id2 = RAGEngine.generate_chunk_id("doc_b.pdf", 0)
        assert id1 != id2

    def test_collection_count_property(self, engine, sample_chunks):
        assert engine.collection_count == 0
        engine.add_chunks(sample_chunks)
        assert engine.collection_count == 4
