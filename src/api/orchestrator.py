"""Orchestrator — runs the full agent pipeline for a Jira webhook event.

The orchestrator ties together all pipeline stages:
  Webhook Event
    → Build Comment model
    → Redact PII (redactor)
    → Classify (CommentClassifier)
    → Collect Context (ContextCollector + Tooling Layer)
    → Draft (ResponseDrafter + LLM)
    → Persist (SQLiteDraftStore)
    → Notify (Teams / Email)
"""

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
)
from src.models.webhook import JiraWebhookEvent
from src.models.comment import Comment
from src.utils.redactor import redact_with_stats
from src.agent.duplicate_detector import DuplicateDetector

logger = logging.getLogger(__name__)

_duplicate_detector = DuplicateDetector()


def _sync_queue_handler(event_dict: dict) -> None:
    """Synchronous queue consumer callback — called by the RabbitMQ daemon thread.

    Deserialises the raw event dict, runs it through the full agent pipeline,
    and logs the result.  Errors are propagated so the broker can nack the
    message (preventing infinite re-delivery).
    """
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
    """Orchestrator — runs the full agent pipeline for a comment event.

    Matches the Orchestrator/Workflow Engine in the architecture diagram:
      Comment → Classify → Context (Tooling Layer) → Draft (LLM) → Store
    """
    _pipeline_start = _t.monotonic()

    assert event.comment is not None
    assert event.issue is not None

    # 1. Build Comment model from webhook event
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

    # 2. Redact PII from comment body before sending to Copilot LLM
    _redaction = redact_with_stats(comment.body)
    _redaction_count = _redaction.redaction_count
    if _redaction_count > 0:
        logger.info(
            "Redacted %d sensitive pattern(s) from comment %s",
            _redaction_count,
            comment.comment_id,
        )
        comment = comment.model_copy(update={"body": _redaction.text})

    # 3. Classify
    classification = classifier.classify(comment)
    logger.info(
        "Classified %s comment %s → %s (%.2f)",
        comment.issue_key,
        comment.comment_id,
        classification.comment_type.value,
        classification.confidence,
    )

    # 4. Context collection (deferred if Jira creds not configured)
    context = _collect_context_safe(comment.issue_key)

    # 4b. Duplicate detection — scan past drafts on the same issue
    _dup_result = _duplicate_detector.check(
        comment_body=comment.body,
        issue_key=comment.issue_key,
        draft_store=draft_store,
    )

    # 4c. Pattern detection — check for 3+ open issues on same component/version
    _pattern_note = _detect_pattern(context)

    # 5. Draft response
    draft = drafter.draft(
        comment,
        classification,
        context,
        redaction_count=_redaction_count,
        pipeline_start_ms=_pipeline_start,
        similar_drafts=_dup_result.to_dict_list() or None,
        pattern_note=_pattern_note,
    )
    logger.info(
        "Generated draft %s for %s (pipeline=%.0f ms, redactions=%d)",
        draft.draft_id,
        comment.issue_key,
        draft.pipeline_duration_ms,
        draft.redaction_count,
    )

    # 6. Store (persistent SQLite)
    draft_store.save(draft, classification=classification.comment_type.value)

    # 7. Notify (Teams / Email — optional, fire-and-forget)
    notifier.notify_draft_generated(
        draft_id=draft.draft_id,
        issue_key=comment.issue_key,
        classification=classification.comment_type.value,
        confidence=classification.confidence,
        body_preview=draft.body,
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
    """Return a pattern_note when 3+ open Jira issues share the same component/version.

    Queries the live Jira API (via ``search_issues``) so the count is always
    fresh.  Returns ``None`` when Jira is unconfigured or the count is < 3.
    """
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

        collector = ContextCollector(
            jira_client=_jira_client,
            log_lookup=_log_lookup,
            testrail_client=_testrail_client,
            git_client=_git_client,
            s3_fetcher=_s3_fetcher if _s3_fetcher.enabled else None,
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
