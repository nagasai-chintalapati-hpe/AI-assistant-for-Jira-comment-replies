"""Orchestrator — runs the full agent pipeline for a webhook event."""

from __future__ import annotations

import asyncio
import logging
import time as _t
from datetime import datetime, timezone

from src.api.deps import (
    classifier,
    drafter,
    draft_store,
    notifier,
    _log_lookup,
    _testrail_client,
    _jira_client,
    _git_client,
    _s3_fetcher,
    _jenkins_client,
    _confluence_client,
    _get_rag_engine,
)
from src.models.webhook import JiraWebhookEvent
from src.models.comment import Comment
from src.utils.redactor import redact_with_stats
from src.agent.duplicate_detector import DuplicateDetector
from src.agent.severity_challenger import SeverityChallenger

logger = logging.getLogger(__name__)

_duplicate_detector = DuplicateDetector()
_severity_challenger = SeverityChallenger()


def _sync_queue_handler(event_dict: dict) -> None:
    event = JiraWebhookEvent(**event_dict)
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_orchestrate(event))
        logger.info(
            "Queue event processed — draft_id=%s issue=%s",
            result.get("draft_id"),
            result.get("issue_key"),
        )
    finally:
        loop.close()


async def _orchestrate(event: JiraWebhookEvent) -> dict:
    """Run the full agent pipeline for a comment event."""
    _pipeline_start = _t.monotonic()

    assert event.comment is not None
    assert event.issue is not None

    # Build Comment model from webhook event
    comment = Comment(
        comment_id=event.comment.id,
        issue_key=event.issue.key,
        author=(
            event.comment.author.displayName
            or event.comment.author.emailAddress
            or "unknown"
        ),
        created=(
            datetime.fromisoformat(
                event.comment.created.replace("+0000", "+00:00")
            )
            if event.comment.created
            else datetime.now(timezone.utc)
        ),
        updated=(
            datetime.fromisoformat(
                event.comment.updated.replace("+0000", "+00:00")
            )
            if event.comment.updated
            else datetime.now(timezone.utc)
        ),
        body=event.comment.body,
    )

    # Redact PII
    _redaction = redact_with_stats(comment.body)
    _redaction_count = _redaction.redaction_count
    if _redaction_count > 0:
        logger.info(
            "Redacted %d sensitive pattern(s) from comment %s",
            _redaction_count,
            comment.comment_id,
        )
        comment = comment.model_copy(update={"body": _redaction.text})

    # Classify
    classification = classifier.classify(comment)
    logger.info(
        "Classified %s comment %s → %s (%.2f)",
        comment.issue_key,
        comment.comment_id,
        classification.comment_type.value,
        classification.confidence,
    )

    # Context collection
    context = _collect_context_safe(comment.issue_key)

    # Duplicate detection
    _dup_result = _duplicate_detector.check(
        comment_body=comment.body,
        issue_key=comment.issue_key,
        draft_store=draft_store,
    )

    # Pattern detection
    _pattern_note = _detect_pattern(context)

    # Severity challenge
    _severity_result = _severity_challenger.evaluate(
        context,
        pattern_note=_pattern_note,
        jira_client=_jira_client,
    )
    _severity_dict = _severity_result.to_dict() if _severity_result else None
    if _severity_result and _severity_result.disagrees:
        logger.warning(
            "Severity challenge active on %s — Rovo set %s, evidence says %s",
            comment.issue_key,
            _severity_result.rovo_changes[-1].to_value,
            _severity_result.recommended_severity,
        )

    # Multi-repo tracking
    _repos_searched = context.repos_searched

    # Draft response
    draft = drafter.draft(
        comment,
        classification,
        context,
        redaction_count=_redaction_count,
        pipeline_start_ms=_pipeline_start,
        similar_drafts=_dup_result.to_dict_list() or None,
        pattern_note=_pattern_note,
        severity_challenge=_severity_dict,
        repos_searched=_repos_searched,
    )
    logger.info(
        "Generated draft %s for %s (pipeline=%.0f ms, redactions=%d)",
        draft.draft_id,
        comment.issue_key,
        draft.pipeline_duration_ms,
        draft.redaction_count,
    )

    # Store in persistent SQLite
    draft_store.save(draft, classification=classification.comment_type.value)

    # Notify human reviewers
    notifier.notify_draft_generated(
        draft_id=draft.draft_id,
        issue_key=comment.issue_key,
        classification=classification.comment_type.value,
        confidence=classification.confidence,
        body_preview=draft.body,
        evidence_links=draft.citations,
        missing_info=draft.missing_info,
    )

    return {
        "status": "processed",
        "event_id": event.event_id,
        "issue_key": comment.issue_key,
        "comment_id": comment.comment_id,
        "classification": classification.comment_type.value,
        "confidence": classification.confidence,
        "draft_id": draft.draft_id,
    }


def _detect_pattern(context) -> str | None:
    """Return a note when 3+ open issues share the same component/version."""
    if _jira_client is None:
        return None

    ctx = context.issue_context
    components = ctx.components or []
    versions = ctx.versions or []

    if not components and not versions:
        return None

    component = components[0] if components else None
    version = versions[0] if versions else None

    jql_parts = ["issuetype in (Bug, Defect)", "status not in (Done, Closed, Resolved)"]
    if component:
        jql_parts.append(f'component = "{component}"')
    if version:
        jql_parts.append(f'affectedVersion = "{version}"')

    try:
        issues = _jira_client.search_issues(
            " AND ".join(jql_parts),
            max_results=50,
            fields=["key", "summary"],
        )
        count = len(issues)
        if count >= 3:
            parts: list[str] = []
            if version:
                parts.append(f"v{version}")
            if component:
                parts.append(component)
            descriptor = " / ".join(parts) if parts else "this area"
            logger.info(
                "Pattern detected: %d open issues on %s", count, descriptor
            )
            return (
                f"Pattern detected: {count} open Bug/Defect issues on "
                f"{descriptor} — possible systemic issue."
            )
    except Exception as exc:
        logger.debug("Pattern check skipped (%s)", exc)

    return None


def _collect_context_safe(issue_key: str):
    """Collect context via the Tooling Layer; return a minimal stub on failure."""
    try:
        from src.agent.context_collector import ContextCollector
        from src.agent.pipeline_correlator import BuildPipelineCorrelator

        _correlator = BuildPipelineCorrelator(
            git_client=_git_client,
            jenkins_client=_jenkins_client if _jenkins_client and _jenkins_client.enabled else None,
            testrail_client=_testrail_client if _testrail_client and _testrail_client.enabled else None,
            confluence_client=_confluence_client,
        )

        collector = ContextCollector(
            jira_client=_jira_client,
            rag_engine=_get_rag_engine(),
            log_lookup=_log_lookup,
            testrail_client=_testrail_client,
            git_client=_git_client,
            s3_fetcher=_s3_fetcher if _s3_fetcher and _s3_fetcher.enabled else None,
            jenkins_client=_jenkins_client if _jenkins_client and _jenkins_client.enabled else None,
            confluence_client=_confluence_client,
            pipeline_correlator=_correlator if _correlator.enabled else None,
        )
        return collector.collect(issue_key)
    except Exception as exc:
        logger.warning("Context collection skipped (%s) – using stub", exc)
        from src.models.context import IssueContext, ContextCollectionResult

        return ContextCollectionResult(
            issue_context=IssueContext(
                issue_key=issue_key,
                summary="",
                description="",
                issue_type="Bug",
                status="Open",
                priority="Medium",
            ),
            collection_timestamp=datetime.now(timezone.utc),
            collection_duration_ms=0.0,
        )
