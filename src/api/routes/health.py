"""Health check, metrics, and deep connectivity routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from src.api import deps as _deps
from src.api.deps import (
    draft_store,
    _rate_limiter,
    _teams,
    _email,
    _git_client,
    _log_lookup,
    _testrail_client,
    _broker,
    _jira_client,
    _s3_fetcher,
    _confluence_client,
    _get_rag_engine,
)
from src.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    """Lightweight health check."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.6.0",
        "drafts_in_store": draft_store.count(),
        "notifications": {
            "teams": _teams.enabled,
            "email": _email.enabled,
        },
        "integrations": {
            "jira": _jira_client is not None,
            "git": _git_client is not None,
            "confluence": _confluence_client is not None,
            "testrail": _testrail_client.enabled,
            "rag": _get_rag_engine() is not None,
            "elk": _log_lookup.elk_enabled,
            "queue": _broker.enabled,
        },
        "optional": {
            "s3": _s3_fetcher.enabled,
            "redis": _rate_limiter._redis is not None,
        },
    }


@router.get("/metrics")
async def get_metrics():
    """Return aggregated draft quality and processing metrics."""
    return draft_store.get_metrics()


@router.get("/metrics/prometheus")
async def get_metrics_prometheus():
    """Expose metrics in Prometheus text format."""
    try:
        from prometheus_client import (
            CollectorRegistry,
            Gauge,
            generate_latest,
            CONTENT_TYPE_LATEST,
        )
        from fastapi.responses import Response
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail=(
                "prometheus-client is not installed. "
                "Run: pip install prometheus-client"
            ),
        )

    raw = draft_store.get_metrics()
    registry = CollectorRegistry()

    status_gauge = Gauge(
        "jira_assistant_drafts_total",
        "Draft count by status",
        ["status"],
        registry=registry,
    )
    status_gauge.labels(status="generated").set(raw["pending"])
    status_gauge.labels(status="approved").set(raw["approved"])
    status_gauge.labels(status="rejected").set(raw["rejected"])

    Gauge(
        "jira_assistant_acceptance_rate_pct",
        "Percentage of drafts approved out of total",
        registry=registry,
    ).set(raw["acceptance_rate_pct"])

    Gauge(
        "jira_assistant_avg_confidence",
        "Mean LLM classification confidence score (0–1)",
        registry=registry,
    ).set(raw["avg_confidence"] or 0.0)

    Gauge(
        "jira_assistant_avg_rating",
        "Mean human quality rating for approved drafts (1–5)",
        registry=registry,
    ).set(raw["avg_rating"] or 0.0)

    Gauge(
        "jira_assistant_hallucination_flagged_total",
        "Number of drafts where hallucination was detected",
        registry=registry,
    ).set(raw["hallucination_flagged"])

    Gauge(
        "jira_assistant_edited_drafts_total",
        "Approved drafts where a human edited the AI-generated body",
        registry=registry,
    ).set(raw.get("drafts_edited_before_approval", 0))

    Gauge(
        "jira_assistant_pct_approved_edited",
        "Percentage of approved drafts that were edited before posting",
        registry=registry,
    ).set(raw.get("pct_approved_drafts_edited", 0.0))

    clf_gauge = Gauge(
        "jira_assistant_drafts_by_classification",
        "Draft count per classification bucket",
        ["classification"],
        registry=registry,
    )
    for clf, count in raw.get("by_classification", {}).items():
        clf_gauge.labels(classification=clf).set(count)

    Gauge(
        "jira_assistant_avg_pipeline_duration_ms",
        "Mean end-to-end pipeline duration in milliseconds",
        registry=registry,
    ).set(raw.get("avg_pipeline_duration_ms") or 0.0)

    Gauge(
        "jira_assistant_total_redactions",
        "Total number of PII/secret patterns scrubbed across all processed comments",
        registry=registry,
    ).set(raw.get("total_redactions", 0))

    return Response(
        content=generate_latest(registry),
        media_type=CONTENT_TYPE_LATEST,
    )


@router.get("/health/deep")
async def deep_health_check():
    """Deep health check — tests live connectivity to each integration."""
    results: dict[str, dict] = {}

    # Jira
    if _jira_client is not None:
        try:
            _jira_client.client.myself()
            results["jira"] = {"status": "ok"}
        except Exception as exc:
            results["jira"] = {"status": "degraded", "error": str(exc)[:120]}
    else:
        results["jira"] = {"status": "disabled"}

    # TestRail
    if _testrail_client.enabled:
        try:
            _testrail_client.get_runs(
                project_id=settings.testrail.project_id or 1,
                limit=1,
            )
            results["testrail"] = {"status": "ok"}
        except Exception as exc:
            results["testrail"] = {"status": "degraded", "error": str(exc)[:120]}
    else:
        results["testrail"] = {"status": "disabled"}

    # RAG / ChromaDB
    try:
        engine = _get_rag_engine()
        stats = engine.stats()
        results["rag"] = {"status": "ok", "chunks": stats.get("total_chunks", 0)}
    except Exception as exc:
        results["rag"] = {"status": "degraded", "error": str(exc)[:120]}

    # Redis
    if _rate_limiter._redis is not None:
        try:
            _rate_limiter._redis.ping()
            results["redis"] = {"status": "ok"}
        except Exception as exc:
            results["redis"] = {"status": "degraded", "error": str(exc)[:120]}
    else:
        results["redis"] = {"status": "disabled"}

    # RabbitMQ queue
    if _broker.enabled:
        results["queue"] = {"status": "ok"}
    else:
        results["queue"] = {"status": "disabled"}

    # SQLite draft store
    try:
        count = draft_store.count()
        results["sqlite"] = {"status": "ok", "drafts": count}
    except Exception as exc:
        results["sqlite"] = {"status": "degraded", "error": str(exc)[:120]}

    overall = (
        "degraded"
        if any(v.get("status") == "degraded" for v in results.values())
        else "ok"
    )
    return {
        "overall": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "integrations": results,
    }
