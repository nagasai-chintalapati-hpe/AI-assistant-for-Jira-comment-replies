"""RAG pipeline routes — ingest, query, search, stats, delete.

Endpoints
---------
POST /rag/ingest/pdf          Upload + ingest a PDF
POST /rag/ingest/text         Ingest raw text
POST /rag/ingest/confluence   Ingest Confluence pages
POST /rag/ingest/jira         Ingest resolved Jira tickets
GET  /rag/search              Semantic search (query-param)
POST /rag/query               Semantic search (JSON body)
GET  /rag/stats               ChromaDB collection stats
DELETE /rag/document/{title}  Remove document chunks
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from src.api.deps import _jira_client, _get_rag_engine, _get_rag_ingester
from src.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/rag/ingest/pdf")
async def rag_ingest_pdf(file: UploadFile = File(...)):
    """Upload and ingest a PDF into the RAG index.

    The file is saved to the configured PDF upload directory, parsed,
    chunked, and indexed into ChromaDB.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are accepted")

    upload_dir = Path(settings.rag.pdf_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / file.filename

    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        ingester = _get_rag_ingester()
        count = ingester.ingest_pdf(str(dest), source_title=file.filename)
        return {"status": "ingested", "filename": file.filename, "chunks_indexed": count}
    except Exception as exc:
        logger.error("PDF ingestion failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")


@router.post("/rag/ingest/text")
async def rag_ingest_text(request: Request):
    """Ingest raw text into the RAG index.

    Payload: ``{"title": str, "text": str, "source_type": str, "url": str|null}``
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    title = payload.get("source_title") or payload.get("title")
    text = payload.get("text")
    if not title or not text:
        raise HTTPException(
            status_code=400, detail="'source_title' and 'text' are required"
        )

    source_type = payload.get("source_type", "text")
    url = payload.get("source_url") or payload.get("url")
    metadata = payload.get("metadata")

    try:
        ingester = _get_rag_ingester()
        count = ingester.ingest_text(
            text=text,
            source_title=title,
            source_type=source_type,
            source_url=url,
            metadata=metadata,
        )
        return {"status": "ingested", "title": title, "chunks_indexed": count}
    except Exception as exc:
        logger.error("Text ingestion failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")


@router.post("/rag/ingest/confluence")
async def rag_ingest_confluence(request: Request):
    """Ingest one or more Confluence pages into the RAG index.

    Payload: ``{"page_ids": [str]}`` or ``{"space_key": str, "label": str|null}``
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    page_ids = payload.get("page_ids", [])
    space_key = payload.get("space_key")
    label = payload.get("label")

    if not page_ids and not space_key:
        raise HTTPException(
            status_code=400,
            detail="Provide 'page_ids' or 'space_key' to ingest",
        )

    from src.integrations.confluence import ConfluenceClient

    conf_client = ConfluenceClient()
    if not conf_client.enabled:
        raise HTTPException(
            status_code=503,
            detail="Confluence is not configured (check env vars)",
        )

    ingester = _get_rag_ingester()
    total_chunks = 0
    pages_ingested = 0

    if space_key and not page_ids:
        pages = conf_client.search_pages(space_key=space_key, label=label)
        page_ids = [p["id"] for p in pages if p.get("id")]

    for pid in page_ids:
        try:
            count = ingester.ingest_confluence_page(pid, confluence_client=conf_client)
            total_chunks += count
            if count > 0:
                pages_ingested += 1
        except Exception as exc:
            logger.warning("Failed to ingest Confluence page %s: %s", pid, exc)

    return {
        "status": "ingested",
        "pages_ingested": pages_ingested,
        "total_chunks_indexed": total_chunks,
    }


@router.post("/rag/ingest/jira")
async def rag_ingest_jira(request: Request):
    """Ingest resolved Jira tickets as prior-defect RAG context.

    Fetches Bug/Defect tickets with statuses Done/Resolved/Closed and indexes
    them with ``source_type="jira"`` so they appear in prior-defect queries.

    Payload (all optional)::

        {"project_keys": ["PROJ"], "max_issues": 100, "statuses": ["Done"]}
    """
    if _jira_client is None:
        raise HTTPException(
            status_code=503,
            detail="Jira is not configured (check JIRA_BASE_URL / credentials)",
        )
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    project_keys: list[str] = payload.get("project_keys") or []
    max_issues: int = int(payload.get("max_issues", 100))
    statuses: Optional[list[str]] = payload.get("statuses") or None

    try:
        ingester = _get_rag_ingester()
        total_chunks = ingester.ingest_jira_resolved(
            jira_client=_jira_client,
            project_keys=project_keys or None,
            max_issues=max_issues,
            statuses=statuses,
        )
        return {
            "status": "ingested",
            "project_keys": project_keys,
            "max_issues": max_issues,
            "total_chunks_indexed": total_chunks,
        }
    except Exception as exc:
        logger.error("Jira RAG ingestion failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")


@router.get("/rag/search")
async def rag_search(q: str, top_k: int = 5, source_type: Optional[str] = None):
    """Search the RAG index for relevant document chunks.

    Query params: ``?q=search+text&top_k=5&source_type=confluence``
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

    engine = _get_rag_engine()
    result = engine.query(text=q, top_k=top_k, source_type=source_type or None)

    return {
        "query": result.query,
        "total_chunks_searched": result.total_chunks_searched,
        "retrieval_duration_ms": result.retrieval_duration_ms,
        "results": [
            {
                "chunk_id": s.chunk_id,
                "source_type": s.source_type,
                "source_title": s.source_title,
                "source_url": s.source_url,
                "content": s.content,
                "relevance_score": s.relevance_score,
            }
            for s in result.snippets
        ],
    }


@router.post("/rag/query")
async def rag_query(request: Request):
    """Semantic search over the RAG index (POST variant of GET /rag/search).

    Accepts a JSON body for richer query options::

        {"query": "login timeout after firmware upgrade", "top_k": 5, "source_type": "confluence"}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    query_text: str = payload.get("query", "")
    if not query_text.strip():
        raise HTTPException(status_code=400, detail="'query' field is required")

    top_k: int = int(payload.get("top_k", settings.rag.top_k))
    source_type: Optional[str] = payload.get("source_type") or None

    engine = _get_rag_engine()
    result = engine.query(text=query_text, top_k=top_k, source_type=source_type)

    return {
        "query": result.query,
        "total_chunks_searched": result.total_chunks_searched,
        "retrieval_duration_ms": result.retrieval_duration_ms,
        "results": [
            {
                "chunk_id": s.chunk_id,
                "source_type": s.source_type,
                "source_title": s.source_title,
                "source_url": s.source_url,
                "content": s.content,
                "relevance_score": s.relevance_score,
            }
            for s in result.snippets
        ],
    }


@router.get("/rag/stats")
async def rag_stats():
    """Return RAG collection statistics."""
    engine = _get_rag_engine()
    return engine.stats()


@router.delete("/rag/document/{source_title}")
async def rag_delete_document(source_title: str):
    """Remove all chunks for a given source document."""
    engine = _get_rag_engine()
    deleted = engine.delete_by_source(source_title)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="No chunks found for this source")
    return {
        "status": "deleted",
        "source_title": source_title,
        "chunks_deleted": deleted,
    }
