"""RAG (Retrieval-Augmented Generation) engine and document ingestion."""

__all__ = ["RAGEngine", "DocumentIngester"]


def __getattr__(name: str):
    if name == "RAGEngine":
        from src.rag.engine import RAGEngine
        return RAGEngine
    if name == "DocumentIngester":
        from src.rag.ingest import DocumentIngester
        return DocumentIngester
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
