"""Draft generator – creates responses using templates + Copilot SDK.

Flow:
  1. Select a response template based on the classification bucket.
  2. Fill the template with context (issue fields, evidence, citations).
  3. Optionally refine via Copilot SDK for natural language polish.
  4. Return a Draft with citations, evidence tracking, and suggested actions.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
from src.models.context import ContextCollectionResult
from src.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

# Response templates — one per classification bucket
TEMPLATES: dict[CommentType, str] = {
    CommentType.CANNOT_REPRODUCE: (
        "Thanks for the update. We were able to reproduce on "
        "**{environment}** (Build {build_version}).\n\n"
        "**Observed:** {observation}\n\n"
        "**Repro steps (minimal):**\n{repro_steps}\n\n"
        "{pr_evidence}"
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
        "**Fix version/commit/build:** {fix_version}\n"
        "{pr_evidence}"
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

# Copilot SDK refinement prompt
_REFINE_SYSTEM = """\
You are a QA engineer writing a reply to a developer comment on a Jira defect.
Rewrite the DRAFT below so it sounds professional, concise, and empathetic.
Keep all factual data (build numbers, links, steps) intact. Do NOT invent facts.
Output ONLY the refined reply – no markdown code fences, no explanation.
"""

class ResponseDrafter:
    """Generates draft responses using templates + Copilot SDK."""

    def __init__(self, llm_client=None, api_key: Optional[str] = None, model: str = "gpt-4"):
        """
        Args:
            llm_client: :class:`~src.llm.client.CopilotLLMClient` instance.
                        If *None*, the module-level singleton is used.
            api_key:    Deprecated — kept for backward compatibility only.
            model:      Deprecated — kept for backward compatibility only.
        """
        if llm_client is None:
            from src.llm.client import get_llm_client
            llm_client = get_llm_client()
        self._llm = llm_client
        if self._llm.enabled:
            logger.info("Copilot SDK drafter initialised (backend=%s)", self._llm.backend)
        else:
            logger.info("Copilot LLM not available — using template-only drafts")

    # Public API
    def draft(
        self,
        comment: Comment,
        classification: CommentClassification,
        context: ContextCollectionResult,
        redaction_count: int = 0,
        pipeline_start_ms: Optional[float] = None,
        similar_drafts: Optional[list[dict]] = None,
        pattern_note: Optional[str] = None,
    ) -> Draft:
        """Generate a draft response to a comment.

        Parameters
        ----------
        comment : Comment
        classification : CommentClassification
        context : ContextCollectionResult
        redaction_count : int
            Number of PII/secret patterns that were redacted before the LLM
            received the comment body.  Stored in the draft audit record.
        pipeline_start_ms : float | None
            ``time.monotonic()`` value captured at the start of the orchestrator
            so the total pipeline wall-clock time can be recorded.
        similar_drafts : list[dict] | None
            Past drafts on the same issue with overlapping content, surfaced in
            the review UI as a "possible duplicate" warning.
        pattern_note : str | None
            Systemic-bug note when 3+ open issues share the same component/version.
        """
        # 1. Template-fill
        template_body = self._fill_template(comment, classification, context)
        # 2. Optional Copilot SDK refinement
        if self._llm.enabled:
            refined = self._refine_with_copilot(template_body, comment)
            draft_body = refined or template_body
        else:
            draft_body = template_body
        # 3. Build citations from context
        citations = self._build_citations(context)
        # 4. Build evidence_used list from RAG snippets
        evidence_used = self._build_evidence_used(context)
        # 5. Hallucination check — flag drafts with specific claims but no evidence
        hallucination_flag = self._detect_hallucination(draft_body, citations)
        # 6. Assemble Draft
        import time as _time_mod
        pipeline_ms = (
            (_time_mod.monotonic() - pipeline_start_ms) * 1000
            if pipeline_start_ms is not None
            else 0.0
        )
        return Draft(
            draft_id=f"draft_{int(datetime.now(timezone.utc).timestamp())}",
            issue_key=comment.issue_key,
            in_reply_to_comment_id=comment.comment_id,
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body=draft_body,
            original_body=draft_body,   # preserved even after human edits
            status=DraftStatus.GENERATED,
            suggested_actions=self._suggest_actions(classification),
            suggested_labels=self._suggest_labels(classification),
            confidence_score=classification.confidence,
            citations=citations,
            evidence_used=evidence_used or None,
            classification_type=classification.comment_type.value,
            classification_reasoning=classification.reasoning,
            hallucination_flag=hallucination_flag,
            redaction_count=redaction_count,
            pipeline_duration_ms=round(pipeline_ms, 1),
            similar_drafts=similar_drafts,
            pattern_note=pattern_note,
        )

    # Copilot SDK refinement
    def _refine_with_copilot(self, draft_text: str, comment: Comment) -> Optional[str]:
        """Polish the template-filled draft using Copilot LLM."""
        return self._llm.complete(
            messages=[
                {"role": "system", "content": _REFINE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Original developer comment on {comment.issue_key}:\n"
                        f'"""\n{comment.body}\n"""\n\n'
                        f"DRAFT reply:\n"
                        f'"""\n{draft_text}\"""'
                    ),
                },
            ],
            max_tokens=512,
            temperature=0.3,
        )

    @staticmethod
    def _detect_hallucination(
        body: str, citations: list[dict[str, str]]
    ) -> bool:
       
        import re

        _CLAIM_PATTERNS = [
            r"Build\s*#\d+",                                # "Build #123"
            r"\bcommit\s+[0-9a-f]{7,40}\b",               # "commit abc1234"
            r"\bv?\d+\.\d+\.\d+[-+.\w]*\b",              # version "1.2.3" / "v2.3.1-rc1"
            r"\b(passed|failed|skipped)\s+\d+\s+tests?\b", # "failed 5 tests"
        ]
        has_claim = any(
            re.search(pat, body, re.IGNORECASE) for pat in _CLAIM_PATTERNS
        )
        if not has_claim:
            return False
        if citations:
            # Evidence citations exist — claims are supported
            return False
        logger.warning(
            "Hallucination flag set — draft contains specific technical claims "
            "but has no supporting evidence citations"
        )
        return True
    
    # Template filling
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
        bm = context.build_metadata or {}
        subs: dict[str, str] = {
            "issue_key": ctx.issue_key,
            "summary": ctx.summary,
            "environment": ctx.environment or "N/A",
            "build_version": bm.get("version") or (ctx.versions[0] if ctx.versions else "N/A"),
            "observation": "See attached evidence",
            "repro_steps": "1. (auto-detected from ticket – please verify)",
            "feature_flag": "N/A",
            "component": (ctx.components[0] if ctx.components else "N/A"),
            "time_window": "last 24 h",
            "existing_evidence": self._format_existing_evidence(context),
            "missing_items": self._format_missing(classification),
            "doc_link": "N/A",
            "expected_behavior": "See referenced documentation",
            "fix_version": bm.get("version") or (ctx.versions[0] if ctx.versions else "N/A"),
            "retest_checklist": self._build_retest_checklist(context),
            "target_env": ctx.environment or "staging",
            "related_ticket": self._find_related_ticket(ctx),
            "related_status": "See linked ticket",
            "blocking_item": self._find_blocking_item(ctx),
            "expected_resolution": "TBD – pending dependency update",
            "expected_config": "See documentation",
        }

        subs["pr_evidence"] = self._format_pr_evidence(context)
        subs["elk_log_preview"] = self._format_elk_preview(context)

        try:
            return template.format_map(subs)
        except KeyError as exc:
            logger.warning("Template substitution key missing: %s", exc)
            return template  # return raw template on failure

    # Evidence and citations
    @staticmethod
    def _format_pr_evidence(context: ContextCollectionResult) -> str:
        """Return a formatted Git PR evidence block (empty string if no PRs)."""
        if not context.git_prs:
            return ""
        lines = ["**Related Git PRs:**"]
        for pr in context.git_prs[:3]:
            sha = f" (commit `{pr.merge_commit_sha}`)" if pr.merge_commit_sha else ""
            branch_info = f" `{pr.head_branch}` → `{pr.base_branch}`" if pr.head_branch else ""
            lines.append(
                f"• PR #{pr.pr_number} [{pr.state}]{sha}{branch_info}: "
                f"[{pr.pr_title}]({pr.pr_url})"
            )
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _format_elk_preview(context: ContextCollectionResult) -> str:
        """Return a short ELK log preview (empty string if no ELK entries)."""
        if not context.elk_log_entries:
            return ""
        lines = ["**ELK log hits:**"]
        for entry in context.elk_log_entries[:3]:
            level = f"[{entry.level}] " if entry.level else ""
            preview = entry.message[:120].replace("\n", " ")
            ts = f" @ {entry.timestamp}" if entry.timestamp else ""
            lines.append(f"• {level}{preview}…{ts}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _format_existing_evidence(context: ContextCollectionResult) -> str:
        """Format attachments, Jenkins links, RAG snippets, logs, and TestRail as bullet list."""
        lines: list[str] = []
        if context.issue_context.attached_files:
            for att in context.issue_context.attached_files[:5]:
                name = att.get("filename") or att.get("name", "attachment")
                lines.append(f"• Attachment: {name}")
        if context.jenkins_links:
            for url in context.jenkins_links[:3]:
                lines.append(f"• Jenkins log: {url}")
        if context.rag_snippets:
            for snippet in context.rag_snippets[:3]:
                source = snippet.source_title
                score = f"{snippet.relevance_score:.0%}"
                preview = snippet.content[:120].replace("\n", " ")
                lines.append(f"• [{source}] ({score}): {preview}…")
        if context.log_entries:
            for entry in context.log_entries[:3]:
                preview = entry.message[:100].replace("\n", " ")
                lines.append(f"• Log ({entry.source}): {preview}…")
        if context.testrail_results:
            for tr in context.testrail_results[:2]:
                name = tr.get("name", "run")
                rate = tr.get("pass_rate", 0)
                failed = tr.get("failed", 0)
                lines.append(f"• TestRail [{name}]: {rate}% pass, {failed} failed")
        if context.git_prs:
            for pr in context.git_prs[:3]:
                sha = f" (commit `{pr.merge_commit_sha}`)" if pr.merge_commit_sha else ""
                lines.append(
                    f"• Git PR #{pr.pr_number} [{pr.state}]{sha}: {pr.pr_title} — {pr.pr_url}"
                )
        if context.elk_log_entries:
            for entry in context.elk_log_entries[:3]:
                preview = entry.message[:100].replace("\n", " ")
                level = f"[{entry.level}] " if entry.level else ""
                lines.append(f"• ELK log: {level}{preview}…")
        return "\n".join(lines) if lines else "• (none collected yet)"

    @staticmethod
    def _format_missing(classification: CommentClassification) -> str:
        """Format missing context items as bullet list."""
        if not classification.missing_context:
            return "• (nothing flagged)"
        return "\n".join(f"• {item}" for item in classification.missing_context)

    @staticmethod
    def _build_retest_checklist(context: ContextCollectionResult) -> str:
        """Build a retest checklist from TestRail results and build metadata."""
        lines: list[str] = []
        lines.append("1. Verify the reported scenario end-to-end")
        if context.testrail_results:
            for tr in context.testrail_results[:1]:
                failed_tests = tr.get("failed_tests", [])
                for i, t in enumerate(failed_tests[:3], start=2):
                    name = t.get("title") or t.get("name", "test case")
                    lines.append(f"{i}. Re-run: {name}")
        if context.build_metadata:
            bm = context.build_metadata
            if bm.get("version"):
                lines.append(f"{len(lines) + 1}. Confirm fix on build {bm['version']}")
        return "\n".join(lines)

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
        """Build citation list from Jenkins links, RAG snippets, logs, and TestRail."""
        citations: list[dict[str, str]] = []
        if context.jenkins_links:
            for url in context.jenkins_links:
                citations.append({"source": "Jenkins", "url": url})
        if context.rag_snippets:
            for snippet in context.rag_snippets:
                citation: dict[str, str] = {
                    "source": snippet.source_title,
                    "type": snippet.source_type,
                }
                if snippet.source_url:
                    citation["url"] = snippet.source_url
                citation["excerpt"] = snippet.content[:200]
                citations.append(citation)
        if context.log_entries:
            for entry in context.log_entries[:5]:
                citations.append({
                    "source": f"Log ({entry.source})",
                    "excerpt": entry.message[:200],
                })
        if context.testrail_results:
            for tr in context.testrail_results:
                citations.append({
                    "source": f"TestRail: {tr.get('name', 'run')}",
                    "url": tr.get("url", ""),
                    "excerpt": f"{tr.get('pass_rate', 0)}% pass, {tr.get('failed', 0)} failed",
                })
        if context.git_prs:
            for pr in context.git_prs:
                citation: dict[str, str] = {
                    "source": f"Git PR #{pr.pr_number} ({pr.provider})",
                    "url": pr.pr_url,
                    "excerpt": (
                        f"{pr.state} — {pr.pr_title}"
                        + (f" | merged commit {pr.merge_commit_sha}" if pr.merge_commit_sha else "")
                        + (f" | branch {pr.head_branch} → {pr.base_branch}" if pr.head_branch else "")
                    ),
                }
                citations.append(citation)
        if context.elk_log_entries:
            for entry in context.elk_log_entries[:5]:
                cid = f" [{entry.correlation_id}]" if entry.correlation_id else ""
                citations.append({
                    "source": f"ELK log{cid}",
                    "excerpt": entry.message[:200],
                })
        return citations

    @staticmethod
    def _build_evidence_used(context: ContextCollectionResult) -> list[str]:
        """Build a human-readable list of evidence sources used."""
        evidence: list[str] = []
        if context.jenkins_links:
            for url in context.jenkins_links:
                evidence.append(f"Jenkins log: {url}")
        if context.rag_snippets:
            for snippet in context.rag_snippets:
                label = f"{snippet.source_type.title()}: {snippet.source_title}"
                if snippet.relevance_score >= 0.5:
                    label += f" (relevance: {snippet.relevance_score:.0%})"
                evidence.append(label)
        if context.log_entries:
            for entry in context.log_entries[:5]:
                cid = entry.correlation_id or entry.source
                evidence.append(f"Log ({entry.source}): {cid}")
        if context.testrail_results:
            for tr in context.testrail_results:
                evidence.append(
                    f"TestRail run: {tr.get('name', 'unknown')} "
                    f"({tr.get('pass_rate', 0)}% pass)"
                )
        if context.build_metadata:
            bm = context.build_metadata
            parts = []
            if bm.get("commit"):
                parts.append(f"commit {bm['commit']}")
            if bm.get("version"):
                parts.append(bm["version"])
            if parts:
                evidence.append(f"Build: {', '.join(parts)}")
        if context.git_prs:
            for pr in context.git_prs:
                label = f"Git PR #{pr.pr_number} ({pr.state}): {pr.pr_title}"
                if pr.merge_commit_sha:
                    label += f" — commit {pr.merge_commit_sha}"
                evidence.append(label)
        if context.elk_log_entries:
            for entry in context.elk_log_entries[:5]:
                cid = entry.correlation_id or "elk"
                evidence.append(f"ELK log ({entry.level or 'INFO'}): {cid}")
        return evidence

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
        if classification.comment_type in label_map:
            labels.append(label_map[classification.comment_type])

        return labels
