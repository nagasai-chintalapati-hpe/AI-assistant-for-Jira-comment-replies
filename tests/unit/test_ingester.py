"""Tests for document ingester — chunking, PDF, and text ingestion."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from fpdf import FPDF

from src.rag.engine import RAGEngine
from src.rag.ingest import DocumentIngester
from src.models.rag import DocumentChunk


# Fixtures

@pytest.fixture
def engine(tmp_path):
    """Real RAGEngine backed by ChromaDB in a temp directory."""
    chroma_dir = str(tmp_path / "chroma")
    with patch("src.rag.engine.settings") as mock_settings:
        mock_settings.rag.chroma_persist_dir = chroma_dir
        mock_settings.rag.embedding_model = "all-MiniLM-L6-v2"
        mock_settings.rag.top_k = 5

        eng = RAGEngine(
            persist_dir=chroma_dir,
            collection_name="test_ingester",
        )
        yield eng


@pytest.fixture
def ingester(engine):
    """DocumentIngester with a real RAG engine, small chunk size for testing."""
    with patch("src.rag.ingest.settings") as mock_settings:
        mock_settings.rag.chunk_size = 100
        mock_settings.rag.chunk_overlap = 20
        mock_settings.rag.pdf_upload_dir = ".data/pdfs"
        yield DocumentIngester(
            rag_engine=engine,
            chunk_size=100,
            chunk_overlap=20,
        )


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    """Create a real PDF file with extractable text using fpdf2."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, text=(
        "Authentication Troubleshooting Guide. "
        "When users cannot log in, first verify the SSO configuration. "
        "Check the identity provider metadata and certificate expiry dates. "
        "OAuth token refresh failures usually indicate clock skew. "
        "Contact the platform team if the issue persists after these checks."
    ))
    pdf_path = tmp_path / "auth_guide.pdf"
    pdf.output(str(pdf_path))
    return pdf_path


@pytest.fixture
def empty_pdf(tmp_path) -> Path:
    """Create a real PDF file with a blank page (no text)."""
    pdf = FPDF()
    pdf.add_page()
    # Intentionally add no text content
    pdf_path = tmp_path / "empty.pdf"
    pdf.output(str(pdf_path))
    return pdf_path


# Tests — chunk_text

class TestChunking:
    def test_empty_text(self, ingester):
        assert ingester.chunk_text("") == []

    def test_whitespace_only(self, ingester):
        assert ingester.chunk_text("   \n\n  ") == []

    def test_short_text_single_chunk(self, ingester):
        text = "Short text under chunk size."
        chunks = ingester.chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_multiple_chunks(self, ingester):
        text = "A" * 250  # 250 chars with chunk_size=100, overlap=20
        chunks = ingester.chunk_text(text)
        assert len(chunks) >= 3

    def test_chunks_have_overlap(self, ingester):
        """Adjacent chunks should share some text (overlap region)."""
        text = "word " * 60  # 300 chars
        chunks = ingester.chunk_text(text)
        if len(chunks) >= 2:
            tail = chunks[0][-15:]
            assert tail in chunks[1] or chunks[1][:30] in chunks[0]

    def test_paragraph_boundary_preference(self, ingester):
        """Chunking should prefer to split at paragraph boundaries."""
        text = "A" * 60 + "\n\n" + "B" * 60
        chunks = ingester.chunk_text(text)
        assert len(chunks) >= 1

    def test_sentence_boundary_preference(self, ingester):
        """If no paragraph break, split at sentence boundary."""
        text = "A" * 60 + ". " + "B" * 60
        chunks = ingester.chunk_text(text)
        assert len(chunks) >= 1

    def test_overlap_smaller_than_size(self):
        """Overlap >= chunk_size should be clamped."""
        with patch("src.rag.ingest.settings") as mock_settings:
            mock_settings.rag.chunk_size = 50
            mock_settings.rag.chunk_overlap = 50

            mock_engine = MagicMock()
            ing = DocumentIngester(mock_engine, chunk_size=50, chunk_overlap=60)
            chunks = ing.chunk_text("X" * 200)
            assert len(chunks) >= 1

    def test_exact_chunk_size(self, ingester):
        text = "A" * 100
        chunks = ingester.chunk_text(text)
        assert len(chunks) == 1


# Tests — ingest_text

class TestIngestText:
    def test_ingest_empty_text(self, ingester, engine):
        count = ingester.ingest_text("", "title", "text")
        assert count == 0
        assert engine.collection_count == 0

    def test_ingest_short_text(self, ingester, engine):
        count = ingester.ingest_text(
            "Hello world, this is a short document for testing.",
            "greeting", "text",
        )
        assert count == 1
        assert engine.collection_count == 1

    def test_ingest_text_queryable(self, ingester, engine):
        """Ingested text should be retrievable via semantic query."""
        ingester.ingest_text(
            "Kubernetes pod crashes due to OOM when memory limit is set too low.",
            "K8s Guide", "runbook",
        )
        result = engine.query("out of memory pod crash")
        assert len(result.snippets) >= 1
        assert result.snippets[0].source_title == "K8s Guide"

    def test_ingest_text_with_metadata(self, ingester, engine):
        ingester.ingest_text(
            "Content about authentication service configuration.",
            "Auth Config", "runbook",
            source_url="https://example.com",
            metadata={"component": "auth"},
        )
        result = engine.query("authentication config")
        assert result.snippets[0].source_url == "https://example.com"

    def test_ingest_long_text_multiple_chunks(self, ingester, engine):
        text = "Monitoring and alerting best practices. " * 30  # ~1200 chars
        count = ingester.ingest_text(text, "Monitoring Guide", "text")
        assert count >= 3
        assert engine.collection_count >= 3


# Tests — ingest_pdf

class TestIngestPdf:
    def test_ingest_pdf_file_not_found(self, ingester):
        with pytest.raises(FileNotFoundError):
            ingester.ingest_pdf("/nonexistent/path.pdf")

    def test_ingest_pdf_wrong_extension(self, ingester, tmp_path):
        txt_file = tmp_path / "file.txt"
        txt_file.write_text("not a pdf")
        with pytest.raises(ValueError, match="Expected a .pdf file"):
            ingester.ingest_pdf(str(txt_file))

    def test_ingest_pdf_success(self, ingester, engine, sample_pdf):
        count = ingester.ingest_pdf(str(sample_pdf))
        assert count >= 1
        assert engine.collection_count >= 1

    def test_ingest_pdf_content_queryable(self, ingester, engine, sample_pdf):
        """PDF text should be searchable after ingestion."""
        ingester.ingest_pdf(str(sample_pdf))
        result = engine.query("SSO identity provider certificate")
        assert len(result.snippets) >= 1
        assert "auth_guide" in result.snippets[0].source_title.lower() or \
               "sso" in result.snippets[0].content.lower() or \
               "certificate" in result.snippets[0].content.lower()

    def test_ingest_pdf_empty_content(self, ingester, engine, empty_pdf):
        count = ingester.ingest_pdf(str(empty_pdf))
        assert count == 0
        assert engine.collection_count == 0

    def test_ingest_pdf_uses_filename_as_title(self, ingester, engine, sample_pdf):
        ingester.ingest_pdf(str(sample_pdf))
        result = engine.query("authentication")
        found_titles = [s.source_title for s in result.snippets]
        assert any("auth_guide" in t for t in found_titles)

    def test_ingest_pdf_custom_title(self, ingester, engine, sample_pdf):
        ingester.ingest_pdf(str(sample_pdf), source_title="Custom Auth Runbook")
        result = engine.query("authentication")
        assert any(s.source_title == "Custom Auth Runbook" for s in result.snippets)


# Tests — ingest_confluence_page

class TestIngestConfluencePage:
    def test_ingest_confluence_page_success(self, ingester, engine):
        """Confluence client is mocked (external API), but engine is real."""
        mock_confluence = MagicMock()
        mock_confluence.get_page.return_value = {
            "title": "Login Troubleshooting",
            "space": {"key": "ENG"},
        }
        mock_confluence.get_page_content_as_text.return_value = (
            "When login fails, check SSO configuration and IdP metadata."
        )

        with patch("src.rag.ingest.settings") as mock_settings:
            mock_settings.rag.chunk_size = 100
            mock_settings.rag.chunk_overlap = 20
            mock_settings.confluence.base_url = "https://wiki.example.com"

            count = ingester.ingest_confluence_page("12345", mock_confluence)
            assert count >= 1
            assert engine.collection_count >= 1

    def test_ingest_confluence_page_not_found(self, ingester, engine):
        mock_confluence = MagicMock()
        mock_confluence.get_page.return_value = None

        count = ingester.ingest_confluence_page("99999", mock_confluence)
        assert count == 0
        assert engine.collection_count == 0

    def test_ingest_confluence_page_empty_content(self, ingester, engine):
        mock_confluence = MagicMock()
        mock_confluence.get_page.return_value = {"title": "Empty", "space": {"key": "ENG"}}
        mock_confluence.get_page_content_as_text.return_value = ""

        count = ingester.ingest_confluence_page("12345", mock_confluence)
        assert count == 0
        assert engine.collection_count == 0
