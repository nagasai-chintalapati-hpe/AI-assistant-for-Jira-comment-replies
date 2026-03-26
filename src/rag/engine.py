"""ChromaDB-backed RAG engine for semantic retrieval."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from src.config import settings
from src.models.rag import DocumentChunk, RAGSnippet, RAGResult

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "jira_assistant_docs"


class RAGEngine:
    """Semantic search engine backed by ChromaDB. """
    def __init__(
        self,
        persist_dir: Optional[str] = None,
        collection_name: str = _COLLECTION_NAME,
    ) -> None:
        self._persist_dir = persist_dir or settings.rag.chroma_persist_dir
        self._collection_name = collection_name

        # Choose persistent or ephemeral client
        if persist_dir is None and self._persist_dir == settings.rag.chroma_persist_dir:
            self._client = chromadb.PersistentClient(
                path=self._persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        else:
            if persist_dir is None:
                # Ephemeral for tests
                self._client = chromadb.EphemeralClient(
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
            else:
                self._client = chromadb.PersistentClient(
                    path=self._persist_dir,
                    settings=ChromaSettings(anonymized_telemetry=False),
                )

        # Embedding function — uses sentence-transformers via ChromaDB
        self._embedding_fn = chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=settings.rag.embedding_model,
        )

        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )

        logger.info(
            "RAG engine ready — collection=%s, chunks=%d",
            self._collection_name,
            self._collection.count(),
        )

    # Ingest

    def add_chunks(self, chunks: list[DocumentChunk]) -> int:
        """Add document chunks to the vector index; returns count inserted."""
        if not chunks:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict] = []

        for chunk in chunks:
            ids.append(chunk.chunk_id)
            documents.append(chunk.content)
            meta = {
                "source_type": chunk.source_type,
                "source_title": chunk.source_title,
                "source_url": chunk.source_url or "",
            }
            if chunk.metadata:
                meta.update(chunk.metadata)
            metadatas.append(meta)

        self._collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )

        logger.info("Upserted %d chunks into RAG index", len(ids))
        return len(ids)

    # Query

    def query(
        self,
        text: str,
        top_k: Optional[int] = None,
        source_type: Optional[str] = None,
    ) -> RAGResult:
        """Retrieve the most relevant chunks for *text*.     """
        k = top_k or settings.rag.top_k
        where_filter = {"source_type": source_type} if source_type else None

        start = time.perf_counter()
        results = self._collection.query(
            query_texts=[text],
            n_results=k,
            where=where_filter,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        snippets: list[RAGSnippet] = []
        if results and results["ids"] and results["ids"][0]:
            for idx, chunk_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][idx] if results["metadatas"] else {}
                distance = results["distances"][0][idx] if results["distances"] else 1.0
                # ChromaDB cosine distance → relevance score (1 - distance)
                relevance = max(0.0, 1.0 - distance)

                snippets.append(
                    RAGSnippet(
                        chunk_id=chunk_id,
                        source_type=meta.get("source_type", "unknown"),
                        source_title=meta.get("source_title", ""),
                        source_url=meta.get("source_url") or None,
                        content=results["documents"][0][idx],
                        relevance_score=round(relevance, 4),
                        metadata={
                            k: v for k, v in meta.items()
                            if k not in ("source_type", "source_title", "source_url")
                        } or None,
                    )
                )
        return RAGResult(
            query=text,
            snippets=snippets,
            total_chunks_searched=self._collection.count(),
            retrieval_duration_ms=round(elapsed_ms, 2),
        )

    # Delete

    def delete_by_source(self, source_title: str) -> int:
        """Remove all chunks matching *source_title*; returns count deleted."""
        # Get matching IDs first
        existing = self._collection.get(
            where={"source_title": source_title},
        )
        if not existing["ids"]:
            return 0

        count = len(existing["ids"])
        self._collection.delete(ids=existing["ids"])
        logger.info("Deleted %d chunks for source '%s'", count, source_title)
        return count

    def delete_by_id(self, chunk_id: str) -> bool:
        """Delete a single chunk by ID.  Returns True if it existed."""
        existing = self._collection.get(ids=[chunk_id])
        if not existing["ids"]:
            return False
        self._collection.delete(ids=[chunk_id])
        return True

    # Stats

    def stats(self) -> dict:
        """Return collection statistics."""
        total = self._collection.count()

        # Get source type distribution
        source_counts: dict[str, int] = {}
        if total > 0:
            all_meta = self._collection.get(include=["metadatas"])
            if all_meta["metadatas"]:
                for meta in all_meta["metadatas"]:
                    st = meta.get("source_type", "unknown")
                    source_counts[st] = source_counts.get(st, 0) + 1

        return {
            "collection_name": self._collection_name,
            "total_chunks": total,
            "persist_dir": self._persist_dir,
            "embedding_model": settings.rag.embedding_model,
            "sources": source_counts,
        }

    # Helpers

    @staticmethod
    def generate_chunk_id(source_title: str, index: int) -> str:
        """Generate a deterministic chunk ID from source title + index."""
        raw = f"{source_title}::{index}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def collection_count(self) -> int:
        """Current number of chunks in the collection."""
        return self._collection.count()
