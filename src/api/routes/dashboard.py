"""Dashboard routes — analytics, access control, and API endpoints."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.api.deps import draft_store
from src.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

_ui_dir = Path(__file__).parent.parent
_templates = Jinja2Templates(directory=str(_ui_dir / "templates"))


# --- Authentication helpers ---

def _token_required() -> bool:
    """Return True when the dashboard is locked behind a token."""
    return bool(settings.dashboard.token)


def _is_authenticated(cookie_value: Optional[str]) -> bool:
    """Check if the cookie matches the configured token."""
    if not _token_required():
        return True  # No token configured → open access
    if not cookie_value:
        return False
    return hmac.compare_digest(cookie_value, settings.dashboard.token)


def _require_auth(request: Request) -> Optional[RedirectResponse]:
    """Return a redirect to /dashboard/login if not authenticated, else None."""
    if not _token_required():
        return None
    cookie_val = request.cookies.get(settings.dashboard.cookie_name)
    if _is_authenticated(cookie_val):
        return None
    return RedirectResponse(url="/dashboard/login", status_code=307)


# --- Login / Logout ---

@router.get("/dashboard/login")
async def dashboard_login_page(request: Request, error: str = ""):
    """Render the dashboard login page."""
    if not _token_required():
        return RedirectResponse(url="/dashboard", status_code=307)
    return _templates.TemplateResponse(
        request,
        "dashboard_login.html",
        {"error": error},
    )


@router.post("/dashboard/login")
async def dashboard_login(request: Request, token: str = Form("")):
    """Validate the token and set a cookie."""
    if not _token_required():
        return RedirectResponse(url="/dashboard", status_code=303)

    if hmac.compare_digest(token, settings.dashboard.token):
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key=settings.dashboard.cookie_name,
            value=token,
            max_age=settings.dashboard.cookie_max_age,
            httponly=True,
            samesite="lax",
        )
        logger.info("Dashboard login successful")
        return response

    logger.warning("Dashboard login failed — wrong token")
    return _templates.TemplateResponse(
        request,
        "dashboard_login.html",
        {"error": "Invalid token. Please try again."},
        status_code=401,
    )


@router.get("/dashboard/logout")
async def dashboard_logout():
    """Clear the dashboard cookie and redirect to login."""
    response = RedirectResponse(url="/dashboard/login", status_code=303)
    response.delete_cookie(key=settings.dashboard.cookie_name)
    return response


# --- Dashboard page ---

@router.get("/dashboard")
async def dashboard_page(request: Request):
    """Render the analytics dashboard with all data pre-computed server-side."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    metrics = draft_store.get_metrics()
    daily_volume = draft_store.get_daily_volume(days=30)
    severity_challenges = draft_store.get_severity_challenges(limit=50)
    top_issues = draft_store.get_top_issues(limit=10)
    repos_stats = draft_store.get_repos_stats()
    response_times = draft_store.get_avg_response_time_by_day(days=30)

    # Process severity items for the table
    severity_items: list[dict] = []
    override_count = 0
    for draft in severity_challenges:
        sc = draft.get("severity_challenge") or {}
        rovo_changes = sc.get("rovo_changes", [])
        latest = rovo_changes[-1] if rovo_changes else {}
        disagrees = sc.get("disagrees", False)
        if disagrees:
            override_count += 1
        severity_items.append({
            "draft_id": draft.get("draft_id"),
            "issue_key": draft.get("issue_key"),
            "created_at": draft.get("created_at", ""),
            "rovo_from": latest.get("from_value", "?"),
            "rovo_to": latest.get("to_value", "?"),
            "recommended": sc.get("recommended_severity", "?"),
            "disagrees": disagrees,
            "confidence": sc.get("confidence", 0),
            "evidence_summary": _summarise_evidence(sc.get("evidence", {})),
        })

    # Derived values for the template
    classifications = metrics.get("by_classification", {})
    max_class_count = max(classifications.values()) if classifications else 1
    time_saved_hours = round(metrics["approved"] * 15 / 60, 1)
    total = metrics["total_drafts"]
    hall_rate = round(metrics["hallucination_flagged"] / total * 100, 1) if total else 0.0
    max_repo_count = max(repos_stats.values()) if repos_stats else 1

    from src.config import settings as _s
    configured_repos = [r.strip() for r in (_s.git.repos or "").split(",") if r.strip()]
    if not configured_repos and _s.git.repo:
        configured_repos = [_s.git.repo]

    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "metrics": metrics,
            "daily_volume": daily_volume,
            "severity_items": severity_items,
            "override_count": override_count,
            "top_issues": top_issues,
            "repos_stats": repos_stats,
            "response_times": response_times,
            "classifications": classifications,
            "max_class_count": max_class_count,
            "time_saved_hours": time_saved_hours,
            "hall_rate": hall_rate,
            "configured_repos": configured_repos,
            "max_repo_count": max_repo_count,
        },
    )


# --- JSON API endpoints (used by polling clients) ---

@router.get("/dashboard/api/summary")
async def api_summary(request: Request):
    """KPI summary for dashboard cards."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    metrics = draft_store.get_metrics()
    challenges = draft_store.get_severity_challenges(limit=100)

    rovo_overrides = sum(
        1 for c in challenges
        if (c.get("severity_challenge") or {}).get("disagrees", False)
    )
    total_challenges = len(challenges)

    # Estimated time saved: ~15 min per approved draft
    time_saved_hours = round(metrics["approved"] * 15 / 60, 1)

    return JSONResponse({
        **metrics,
        "rovo_overrides": rovo_overrides,
        "total_severity_challenges": total_challenges,
        "estimated_time_saved_hours": time_saved_hours,
    })


@router.get("/dashboard/api/daily-volume")
async def api_daily_volume(request: Request, days: int = 30):
    """Daily draft volume for the trend chart."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    data = draft_store.get_daily_volume(days=days)
    return JSONResponse({"days": days, "data": data})


@router.get("/dashboard/api/classifications")
async def api_classifications(request: Request):
    """Classification breakdown for the pie/doughnut chart."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    metrics = draft_store.get_metrics()
    return JSONResponse({"by_classification": metrics.get("by_classification", {})})


@router.get("/dashboard/api/severity")
async def api_severity_challenges(request: Request, limit: int = 50):
    """Severity challenge log for the Rovo override table."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    challenges = draft_store.get_severity_challenges(limit=limit)
    items = []
    for draft in challenges:
        sc = draft.get("severity_challenge") or {}
        rovo_changes = sc.get("rovo_changes", [])
        latest = rovo_changes[-1] if rovo_changes else {}
        items.append({
            "draft_id": draft.get("draft_id"),
            "issue_key": draft.get("issue_key"),
            "created_at": draft.get("created_at"),
            "rovo_from": latest.get("from_value", "?"),
            "rovo_to": latest.get("to_value", "?"),
            "recommended": sc.get("recommended_severity", "?"),
            "disagrees": sc.get("disagrees", False),
            "confidence": sc.get("confidence", 0),
            "evidence_summary": _summarise_evidence(sc.get("evidence", {})),
        })
    return JSONResponse({"total": len(items), "items": items})


@router.get("/dashboard/api/top-issues")
async def api_top_issues(request: Request, limit: int = 10):
    """Top issues by draft count."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    data = draft_store.get_top_issues(limit=limit)
    return JSONResponse({"items": data})


@router.get("/dashboard/api/repos")
async def api_repos(request: Request):
    """Multi-repo PR search stats."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    data = draft_store.get_repos_stats()
    # Also include configured repos from settings
    from src.config import settings as _s
    configured = [r.strip() for r in (_s.git.repos or "").split(",") if r.strip()]
    if not configured and _s.git.repo:
        configured = [_s.git.repo]

    return JSONResponse({
        "configured_repos": configured,
        "search_counts": data,
    })


@router.get("/dashboard/api/response-time")
async def api_response_time(request: Request, days: int = 30):
    """Average pipeline latency by day."""
    redirect = _require_auth(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    data = draft_store.get_avg_response_time_by_day(days=days)
    return JSONResponse({"days": days, "data": data})


def _summarise_evidence(ev: dict) -> str:
    """Build a one-line summary of severity evidence."""
    parts: list[str] = []
    if ev.get("outage_keyword_matches"):
        parts.append(f"{len(ev['outage_keyword_matches'])} outage keywords")
    if ev.get("customer_escalation"):
        parts.append("customer escalation")
    if ev.get("pattern_count", 0) >= 3:
        parts.append(f"{ev['pattern_count']} similar issues")
    if ev.get("jenkins_failure_count", 0):
        parts.append(f"{ev['jenkins_failure_count']} Jenkins failures")
    if ev.get("testrail_failure_count", 0):
        parts.append(f"{ev['testrail_failure_count']} TestRail failures")
    return ", ".join(parts) if parts else "no strong signals"
