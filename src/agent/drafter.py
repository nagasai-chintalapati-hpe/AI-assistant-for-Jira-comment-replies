"""Draft generator -- creates evidence-based responses from Jira context.

Flow:
  1. Select a response template based on the classification bucket.
  2. Fill the template with context (issue fields, evidence, citations).
  3. Optionally refine via Copilot SDK for natural language polish.
  4. Return a Draft with citations, evidence tracking, and suggested actions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.models.classification import CommentClassification, CommentType
from src.models.comment import Comment
from src.models.context import ContextCollectionResult
from src.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

#  Response templates – one per classification bucket 
TEMPLATES: dict[CommentType, str] = {
    CommentType.CANNOT_REPRODUCE: (
        "Thanks for the update. We were able to reproduce on "
        "**{environment}** (Build {build_version}).\n\n"
        "**Observed:** {observation}\n\n"
        "**Repro steps (minimal):**\n{repro_steps}\n\n"
        "Could you confirm:\n"
        "• Which build/version did you test on?\n"
        "• Your environment (OS, browser, tenant config)?\n"
        "• Whether feature flag `{feature_flag}` is enabled?\n\n"
        "**Next:** If you share your test env/build, we can validate parity; "
        "otherwise we recommend retesting on the latest staging build."
    ),
    CommentType.NEED_MORE_INFO: (
        "Thanks for flagging this. Here's what we already have:\n"
        "{existing_evidence}\n\n"
        "We're still missing:\n{missing_items}\n\n"
        "Could you please provide:\n"
        "• Exact repro steps + correlation IDs\n"
        "• Logs for **{component}** in the time window **{time_window}**\n\n"
        "Once we have this, we'll be able to narrow down the root cause."
    ),
    CommentType.BY_DESIGN: (
        "Thanks for raising this. Based on the specification, this is "
        "**expected behavior**.\n\n"
        "**Reference:** {doc_link}\n\n"
        "The documented behavior states:\n> {expected_behavior}\n\n"
        "If you believe this doesn't match the acceptance criteria, could you "
        "point us to the specific AC? We can then assess whether a doc update "
        "or UX clarification is needed."
    ),
    CommentType.FIXED_VALIDATE: (
        "A fix has been deployed.\n\n"
        "**Fix version/commit/build:** {fix_version}\n\n"
        "**Focused retest checklist:**\n{retest_checklist}\n\n"
        "Please verify in **{target_env}** and update the ticket status."
    ),
    CommentType.DUPLICATE_FIXED: (
        "This issue appears to be a **duplicate** or has already been "
        "addressed.\n\n"
        "**Related ticket:** {related_ticket}\n"
        "**Status:** {related_status}\n\n"
        "If the fix in the related ticket does not fully address your "
        "scenario, please reopen with:\n"
        "• Steps that still reproduce the issue\n"
        "• Environment and build version\n"
        "• How the behavior differs from the related fix\n\n"
        "We’ll re-evaluate once we have those details."
    ),
    CommentType.BLOCKED_WAITING: (
        "Understood — this is currently **blocked / waiting** on an "
        "external dependency.\n\n"
        "**Blocking item:** {blocking_item}\n"
        "**Expected resolution:** {expected_resolution}\n\n"
        "We’ll keep this ticket in its current state until the blocker "
        "is resolved. In the meantime:\n"
        "• Could you confirm the blocking ticket/dependency is still accurate?\n"
        "• Is there a workaround we should document?\n\n"
        "We’ll follow up once the dependency is cleared."
    ),
    CommentType.CONFIG_ISSUE: (
        "Based on our investigation, this appears to be a "
        "**configuration / setup issue** rather than a code defect.\n\n"
        "**Expected configuration:** {expected_config}\n"
        "**Reference:** {doc_link}\n\n"
        "Could you verify:\n"
        "• Your current configuration matches the documented setup?\n"
        "• Any environment-specific overrides are correctly applied?\n\n"
        "If the issue persists after correcting the configuration, please "
        "provide updated logs and we’ll re-investigate."
    ),

    CommentType.OTHER: (
        "Thank you for your comment. We're reviewing this and will "
        "follow up shortly.\n\n"
        "**Issue:** {issue_key} – {summary}"
    ),
}
#  Copilot SDK refinement prompt 

_REFINE_SYSTEM = """\
You are a QA engineer writing a reply to a developer comment on a Jira defect.
Rewrite the DRAFT below so it sounds professional, concise, and empathetic.
Keep all factual data (build numbers, links, steps) intact. Do NOT invent facts.
Output ONLY the refined reply – no markdown code fences, no explanation.
"""


class ResponseDrafter:
    """Generates evidence-based draft responses with optional LLM refinement."""

    def __init__(
        self,
        api_key=None,
        model="claude-sonnet-4.5",
        provider=None,
        base_url=None,
        llm_api_key=None,
        github_token=None,
    ):
        self._client = None
        self._model = model
        self._provider = (provider or os.getenv("LLM_PROVIDER", "copilot")).lower()
        self._base_url = (base_url or os.getenv("LLM_BASE_URL", "http://localhost:8080")).rstrip("/")
        self._llm_api_key = llm_api_key or os.getenv("LLM_API_KEY", "")

        token = github_token or api_key
        if self._provider == "copilot" and token:
            try:
                from copilot import CopilotClient
                self._client = CopilotClient({"github_token": token})
                logger.info("Copilot SDK drafter initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using template-only mode")
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            logger.info(
                "Local LLM provider enabled for drafter (provider=%s, base=%s)",
                self._provider, self._base_url,
            )

    async def draft(self, comment, classification, context):
        """Generate an evidence-based draft response."""
        # 1. Extract structured evidence from context
        evidence = _Evidence(comment, classification, context)

        # 2. Build draft body from evidence
        content = _DraftBuilder(evidence).build()

        # 3. Build citations from real evidence sources
        citations = self._build_citations(context)

        # 4. Optionally refine with LLM
        if self._provider == "copilot" and self._client:
            refined = await self._refine_with_copilot(content)
            if refined:
                content = refined
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            refined = await self._refine_with_local_llm(content)
            if refined:
                content = refined

        return Draft(
            draft_id="draft_" + uuid.uuid4().hex[:12],
            issue_key=comment.issue_key,
            in_reply_to_comment_id=comment.comment_id,
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body=content,
            status=DraftStatus.GENERATED,
            citations=citations,
            suggested_actions=[{"action": a} for a in self._suggest_actions(classification)],
            confidence_score=classification.confidence,
        )

    async def _refine_with_copilot(self, draft_text):
        """Refine draft using Copilot SDK session."""
        session = None
        try:
            session = await self._client.create_session({
                "model": self._model,
                "available_tools": [],
                "system_message": {
                    "mode": "replace",
                    "content": _REFINE_SYSTEM,
                },
            })

            response = await session.send_and_wait({
                "prompt": draft_text,
            })

            if not response or not response.data or not response.data.content:
                return None

            return response.data.content.strip()
        except Exception as e:
            logger.warning("Copilot refinement failed: %s", e)
            return None
    
    #  Template filling                                                
    def _fill_template(
        self,
        comment: Comment,
        classification: CommentClassification,
        context: ContextCollectionResult,
    ) -> str:
        """Select and fill the template for *classification.comment_type*."""

        ctx = context.issue_context
        template = TEMPLATES.get(classification.comment_type, TEMPLATES[CommentType.OTHER])

        # Build a safe substitution dict with fallbacks
        subs: dict[str, str] = {
            "issue_key": ctx.issue_key,
            "summary": ctx.summary,
            "environment": ctx.environment or "N/A",
            "build_version": (ctx.versions[0] if ctx.versions else "N/A"),
            "observation": "See attached evidence",
            "repro_steps": "1. (auto-detected from ticket – please verify)",
            "feature_flag": "N/A",
            "component": (ctx.components[0] if ctx.components else "N/A"),
            "time_window": "last 24 h",
            "existing_evidence": self._format_existing_evidence(context),
            "missing_items": self._format_missing(classification),
            "doc_link": "N/A",
            "expected_behavior": "See referenced documentation",
            "fix_version": (ctx.versions[0] if ctx.versions else "N/A"),
            "retest_checklist": "1. Verify the reported scenario end-to-end",
            "target_env": ctx.environment or "staging",
            "related_ticket": self._find_related_ticket(ctx),
            "related_status": "See linked ticket",
            "blocking_item": self._find_blocking_item(ctx),
            "expected_resolution": "TBD – pending dependency update",
            "expected_config": "See documentation",
        }

        try:
            loop = asyncio.get_running_loop()

            def _do_request():
                headers = {"Content-Type": "application/json"}
                if self._llm_api_key:
                    headers["Authorization"] = "Bearer " + self._llm_api_key
                resp = requests.post(
                    self._base_url + "/v1/chat/completions",
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": _REFINE_SYSTEM},
                            {"role": "user", "content": draft_text},
                        ],
                        "max_tokens": 512,
                        "temperature": 0.3,
                    },
                    headers=headers,
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
                return payload["choices"][0]["message"]["content"].strip()

            return await loop.run_in_executor(None, _do_request)
        except Exception as e:
            logger.warning("Local LLM refinement failed: %s", e)
            return None

    @staticmethod
    def _build_citations(context):
        """Build citations list from real evidence sources."""
        citations = []
        issue = context.issue_context if context else None

        if issue and issue.attached_files:
            for att in issue.attached_files[:5]:
                citations.append({
                    "source": "Attachment: " + att.get("filename", "unknown"),
                    "url": att.get("content_url", ""),
                    "excerpt": att.get("mime_type", "") + " (" + str(att.get("size", 0)) + " bytes)",
                })

        if context and context.jenkins_links:
            snippets = context.jenkins_log_snippets or {}
            for link in context.jenkins_links[:3]:
                snippet = snippets.get(link, "")
                excerpt = snippet[-500:] if snippet else "Console output from CI build"
                citations.append({
                    "source": "Jenkins Build Log",
                    "url": link,
                    "excerpt": excerpt,
                })

        if issue and issue.linked_issues:
            for li in issue.linked_issues[:3]:
                citations.append({
                    "source": "Linked Issue: " + li.get("key", ""),
                    "url": "",
                    "excerpt": li.get("type", "") + " - " + li.get("status", ""),
                })

    @staticmethod
    def _find_related_ticket(ctx) -> str:
        """Extract the first linked issue key (for duplicate references)."""
        if ctx.linked_issues:
            return ctx.linked_issues[0].get("key", "N/A")
        return "N/A"

    @staticmethod
    def _find_blocking_item(ctx) -> str:
        """Extract the first blocking linked issue (for blocked/waiting)."""
        if ctx.linked_issues:
            for link in ctx.linked_issues:
                if link.get("type", "").lower() in ("blocks", "is blocked by"):
                    return link.get("key", "N/A")
            return ctx.linked_issues[0].get("key", "N/A")
        return "N/A – please specify the blocking issue"

    @staticmethod
    def _build_citations(context: ContextCollectionResult) -> list[dict[str, str]]:
        """Build citation list from Jenkins links and other sources."""
        citations: list[dict[str, str]] = []
        if context.jenkins_links:
            for url in context.jenkins_links:
                citations.append({"source": "Jenkins", "url": url})
        return citations

    # Suggested labels & actions

    @staticmethod
    def _suggest_actions(classification: CommentClassification) -> list[dict[str, str]]:
        """Suggest Jira actions based on classification."""
        actions: list[dict[str, str]] = []
        ctype = classification.comment_type

        if ctype == CommentType.FIXED_VALIDATE:
            actions.append({"action": "transition", "value": "Ready for QA"})
        elif ctype == CommentType.CANNOT_REPRODUCE:
            actions.append({"action": "request_info", "value": "environment"})
        elif ctype == CommentType.DUPLICATE_FIXED:
            actions.append({"action": "transition", "value": "Closed"})
            actions.append({"action": "link_issue", "value": "duplicate"})
        elif ctype == CommentType.BLOCKED_WAITING:
            actions.append({"action": "transition", "value": "Blocked"})
        elif ctype == CommentType.CONFIG_ISSUE:
            actions.append({"action": "add_label", "value": "config-issue"})

        return actions

    @staticmethod
    def _suggest_labels(classification: CommentClassification) -> list[str]:
        """Suggest labels based on classification."""
        labels: list[str] = []

        if classification.missing_context:
            labels.append("needs-info")

        label_map: dict[CommentType, str] = {
            CommentType.CANNOT_REPRODUCE: "cannot-reproduce",
            CommentType.FIXED_VALIDATE: "fixed-validate",
            CommentType.BY_DESIGN: "by-design",
            CommentType.DUPLICATE_FIXED: "duplicate",
            CommentType.BLOCKED_WAITING: "blocked",
            CommentType.CONFIG_ISSUE: "config-issue",
        }
        return mapping.get(classification.comment_type, [])
