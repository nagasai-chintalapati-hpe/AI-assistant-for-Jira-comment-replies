"""Draft generator — templates + Copilot SDK refinement."""

from __future__ import annotations
import logging
import re
import time as _time_mod
from datetime import datetime, timezone
from typing import Optional
from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
from src.models.context import ContextCollectionResult
from src.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

# Response templates per classification bucket
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
        "We'll re-evaluate once we have those details."
    ),
    CommentType.BLOCKED_WAITING: (
        "Understood — this is currently **blocked / waiting** on an "
        "external dependency.\n\n"
        "**Blocking item:** {blocking_item}\n"
        "**Expected resolution:** {expected_resolution}\n\n"
        "We'll keep this ticket in its current state until the blocker "
        "is resolved. In the meantime:\n"
        "• Could you confirm the blocking ticket/dependency is still accurate?\n"
        "• Is there a workaround we should document?\n\n"
        "We'll follow up once the dependency is cleared."
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
        "provide updated logs and we'll re-investigate."
    ),
    CommentType.OTHER: (
        "Thank you for your comment. We're reviewing this and will "
        "follow up shortly.\n\n"
        "**Issue:** {issue_key} – {summary}"
    ),
}

_REFINE_SYSTEM = """\
You are a senior QA engineer writing a reply to a comment on a Jira defect ticket.

Your job is to rewrite the DRAFT below into a professional, concise, and helpful reply.

Rules:
1. Ground every claim in the EVIDENCE provided — do NOT invent facts, builds, links, or test results.
2. Reference specific evidence (PR numbers, TestRail pass rates, log excerpts, Jenkins builds) when available.
3. Replace generic placeholders like "N/A", "See attached evidence", or "TBD" with real data from the EVIDENCE, or remove the line entirely if no data exists.
4. Keep the tone professional, empathetic, and action-oriented.
5. If evidence is thin, honestly say what's missing and ask for it.
6. Keep the reply concise — no filler paragraphs.
7. Output ONLY the final reply — no markdown code fences, no explanation, no preamble.
"""

# Suggested-action and label mappings
_ACTION_MAP: dict[CommentType, list[dict[str, str]]] = {
    CommentType.FIXED_VALIDATE: [{"action": "transition", "value": "Ready for QA"}],
    CommentType.CANNOT_REPRODUCE: [{"action": "request_info", "value": "environment"},
                                   {"action": "assign", "value": "reporter"}],
    CommentType.NEED_MORE_INFO: [{"action": "add_label", "value": "needs-info"},
                                 {"action": "assign", "value": "reporter"}],
    CommentType.DUPLICATE_FIXED: [{"action": "transition", "value": "Closed"},
                                  {"action": "link_issue", "value": "duplicate"}],
    CommentType.BLOCKED_WAITING: [{"action": "transition", "value": "Blocked"}],
    CommentType.CONFIG_ISSUE: [{"action": "add_label", "value": "config-issue"}],
    CommentType.OTHER: [{"action": "assign", "value": "qa-lead"}],
}

_LABEL_MAP: dict[CommentType, str] = {
    CommentType.CANNOT_REPRODUCE: "cannot-reproduce",
    CommentType.FIXED_VALIDATE: "fixed-validate",
    CommentType.BY_DESIGN: "by-design",
    CommentType.DUPLICATE_FIXED: "duplicate",
    CommentType.BLOCKED_WAITING: "blocked",
    CommentType.CONFIG_ISSUE: "config-issue",
}


class ResponseDrafter:
    """Generates draft responses using templates + Copilot SDK."""

    def __init__(self, llm_client=None, api_key: Optional[str] = None, model: str = "gpt-4"):
        if llm_client is None:
            from src.llm.client import get_llm_client
            llm_client = get_llm_client()
        self._llm = llm_client
        mode = f"backend={self._llm.backend}" if self._llm.enabled else "template-only"
        logger.info("Drafter initialised (%s)", mode)

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
        severity_challenge: Optional[dict] = None,
        repos_searched: Optional[list[str]] = None,
    ) -> Draft:
        """Generate a draft response for the given comment and context."""
        template_body = self._fill_template(comment, classification, context)
        if self._llm.enabled:
            draft_body = self._refine_with_copilot(
                template_body, comment, classification, context,
            ) or template_body
        else:
            draft_body = template_body

        citations = self._build_citations(context)
        evidence_used = self._build_evidence_used(context)
        pipeline_ms = (
            (_time_mod.monotonic() - pipeline_start_ms) * 1000
            if pipeline_start_ms is not None else 0.0
        )
        return Draft(
            draft_id=f"draft_{int(datetime.now(timezone.utc).timestamp())}",
            issue_key=comment.issue_key,
            in_reply_to_comment_id=comment.comment_id,
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body=draft_body,
            original_body=draft_body,
            status=DraftStatus.GENERATED,
            suggested_actions=list(_ACTION_MAP.get(classification.comment_type, [])),
            suggested_labels=self._suggest_labels(classification),
            confidence_score=classification.confidence,
            citations=citations,
            evidence_used=evidence_used or None,
            classification_type=classification.comment_type.value,
            classification_reasoning=classification.reasoning,
            hallucination_flag=self._detect_hallucination(draft_body, citations),
            redaction_count=redaction_count,
            pipeline_duration_ms=round(pipeline_ms, 1),
            trigger_comment_body=comment.body,
            similar_drafts=similar_drafts,
            pattern_note=pattern_note,
            severity_challenge=severity_challenge,
            repos_searched=repos_searched,
        )

    # LLM refinement

    def _refine_with_copilot(self, draft_text, comment, classification, context):
        """Polish the template-filled draft using Copilot LLM."""
        ctx = context.issue_context
        user_prompt = (
            f"=== TICKET ===\n"
            f"Key: {ctx.issue_key}\n"
            f"Summary: {ctx.summary}\n"
            f"Type: {ctx.issue_type} | Status: {ctx.status} | Priority: {ctx.priority}\n"
            f"Environment: {ctx.environment or 'not specified'}\n"
            f"Components: {', '.join(ctx.components) if ctx.components else 'none'}\n"
            f"Labels: {', '.join(ctx.labels) if ctx.labels else 'none'}\n\n"
            f"Description:\n{(ctx.description or 'No description.')[:1500]}\n\n"
            f"=== CLASSIFICATION ===\n"
            f"Type: {classification.comment_type.value}\n"
            f"Confidence: {classification.confidence:.0%}\n"
            f"Reasoning: {classification.reasoning}\n"
            f"Missing info: {', '.join(classification.missing_context) if classification.missing_context else 'none'}\n\n"
            f"=== EVIDENCE COLLECTED ===\n"
            f"{self._format_existing_evidence(context)}\n"
            f"{self._format_pr_evidence(context)}\n"
            f"=== DEVELOPER COMMENT (replying to this) ===\n"
            f"{comment.body}\n\n"
            f"=== DRAFT TO REWRITE ===\n"
            f"{draft_text}"
        )
        return self._llm.complete(
            messages=[
                {"role": "system", "content": _REFINE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,
            temperature=0.3,
        )

    # Hallucination detection

    @staticmethod
    def _detect_hallucination(body: str, citations: list[dict[str, str]]) -> bool:
        """Flag drafts with specific claims but no supporting citations."""
        patterns = [
            r"Build\s*#\d+", r"\bcommit\s+[0-9a-f]{7,40}\b",
            r"\bv?\d+\.\d+\.\d+[-+.\w]*\b",
            r"\b(passed|failed|skipped)\s+\d+\s+tests?\b",
        ]
        if not any(re.search(p, body, re.IGNORECASE) for p in patterns):
            return False
        if citations:
            return False
        logger.warning("Hallucination flag — claims without evidence citations")
        return True

    # Template filling

    def _fill_template(self, comment, classification, context):
        """Select and fill the template for the classification type."""
        ctx = context.issue_context
        template = TEMPLATES.get(classification.comment_type, TEMPLATES[CommentType.OTHER])
        bm = context.build_metadata or {}
        build_ver = (
            bm.get("version")
            or (ctx.versions[0] if ctx.versions else None)
            or self._extract_build_from_text(ctx.description, ctx.summary, comment.body)
            or "N/A"
        )
        env = ctx.environment or self._extract_env_from_text(ctx.description, comment.body) or "N/A"

        subs = {
            "issue_key": ctx.issue_key, "summary": ctx.summary,
            "environment": env, "build_version": build_ver,
            "observation": "See attached evidence",
            "repro_steps": "1. (auto-detected from ticket – please verify)",
            "feature_flag": "N/A",
            "component": ctx.components[0] if ctx.components else "N/A",
            "time_window": "last 24 h",
            "existing_evidence": self._format_existing_evidence(context),
            "missing_items": self._format_missing(classification),
            "doc_link": "N/A", "expected_behavior": "See referenced documentation",
            "fix_version": bm.get("version") or (ctx.versions[0] if ctx.versions else None) or build_ver,
            "retest_checklist": self._build_retest_checklist(context),
            "target_env": env if env != "N/A" else "staging",
            "related_ticket": self._find_related_ticket(ctx),
            "related_status": "See linked ticket",
            "blocking_item": self._find_blocking_item(ctx),
            "expected_resolution": "TBD – pending dependency update",
            "expected_config": "See documentation",
            "pr_evidence": self._format_pr_evidence(context),
            "elk_log_preview": self._format_elk_preview(context),
        }
        try:
            return template.format_map(subs)
        except KeyError as exc:
            logger.warning("Template substitution key missing: %s", exc)
            return template

    # Evidence formatters

    @staticmethod
    def _format_pr_evidence(context: ContextCollectionResult) -> str:
        if not context.git_prs:
            return ""
        lines = ["**Related Git PRs:**"]
        for pr in context.git_prs[:3]:
            sha = f" (commit `{pr.merge_commit_sha}`)" if pr.merge_commit_sha else ""
            branch = f" `{pr.head_branch}` → `{pr.base_branch}`" if pr.head_branch else ""
            lines.append(f"• PR #{pr.pr_number} [{pr.state}]{sha}{branch}: [{pr.pr_title}]({pr.pr_url})")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _format_elk_preview(context: ContextCollectionResult) -> str:
        if not context.elk_log_entries:
            return ""
        lines = ["**ELK log hits:**"]
        for e in context.elk_log_entries[:3]:
            lvl = f"[{e.level}] " if e.level else ""
            lines.append(f"• {lvl}{e.message[:120].replace(chr(10), ' ')}… @ {e.timestamp or ''}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _format_existing_evidence(context: ContextCollectionResult) -> str:
        """Format all evidence sources as a bullet list."""
        lines: list[str] = []
        ctx = context.issue_context
        for att in (ctx.attached_files or [])[:5]:
            lines.append(f"• Attachment: {att.get('filename') or att.get('name', 'attachment')}")
        for url in (context.jenkins_links or [])[:3]:
            lines.append(f"• Jenkins log: {url}")
        for s in (context.rag_snippets or [])[:3]:
            lines.append(f"• [{s.source_title}] ({s.relevance_score:.0%}): {s.content[:120].replace(chr(10), ' ')}…")
        for e in (context.log_entries or [])[:3]:
            lines.append(f"• Log ({e.source}): {e.message[:100].replace(chr(10), ' ')}…")
        for tr in (context.testrail_results or [])[:2]:
            lines.append(f"• TestRail [{tr.get('name', 'run')}]: {tr.get('pass_rate', 0)}% pass, {tr.get('failed', 0)} failed")
        for pr in (context.git_prs or [])[:3]:
            sha = f" (commit `{pr.merge_commit_sha}`)" if pr.merge_commit_sha else ""
            lines.append(f"• Git PR #{pr.pr_number} [{pr.state}]{sha}: {pr.pr_title} — {pr.pr_url}")
        for e in (context.elk_log_entries or [])[:3]:
            lvl = f"[{e.level}] " if e.level else ""
            lines.append(f"• ELK log: {lvl}{e.message[:100].replace(chr(10), ' ')}…")
        return "\n".join(lines) if lines else "• (none collected yet)"

    @staticmethod
    def _format_missing(classification: CommentClassification) -> str:
        if not classification.missing_context:
            return "• (nothing flagged)"
        return "\n".join(f"• {item}" for item in classification.missing_context)

    @staticmethod
    def _build_retest_checklist(context: ContextCollectionResult) -> str:
        lines = ["1. Verify the reported scenario end-to-end"]
        if context.testrail_results:
            for t in context.testrail_results[:1]:
                for i, tc in enumerate(t.get("failed_tests", [])[:3], start=2):
                    lines.append(f"{i}. Re-run: {tc.get('title') or tc.get('name', 'test case')}")
        bm = context.build_metadata
        if bm and bm.get("version"):
            lines.append(f"{len(lines) + 1}. Confirm fix on build {bm['version']}")
        return "\n".join(lines)

    @staticmethod
    def _find_related_ticket(ctx) -> str:
        return ctx.linked_issues[0].get("key", "N/A") if ctx.linked_issues else "N/A"

    @staticmethod
    def _find_blocking_item(ctx) -> str:
        if not ctx.linked_issues:
            return "N/A – please specify the blocking issue"
        for link in ctx.linked_issues:
            if link.get("type", "").lower() in ("blocks", "is blocked by"):
                return link.get("key", "N/A")
        return ctx.linked_issues[0].get("key", "N/A")

    @staticmethod
    def _extract_build_from_text(*texts: str) -> Optional[str]:
        """Extract a build/version number from free-form text."""
        combined = " ".join(t for t in texts if t)
        if not combined:
            return None
        for pat in [
            r"(?:build|build[_\- ]?(?:number|num|no|id|ver))\s*[:#]?\s*(\d{2,})",
            r"\bv?(\d+\.\d+\.\d+(?:[-+.]\w+)?)\b",
            r"(?:version|release)\s*[:#]?\s*(\d[\d.]+\w*)",
        ]:
            m = re.search(pat, combined, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _extract_env_from_text(*texts: str) -> Optional[str]:
        """Extract an environment name from free-form text."""
        combined = " ".join(t for t in texts if t)
        if not combined:
            return None
        m = re.search(r"(?:environment|env)\s*[:#]?\s*([A-Za-z0-9_-]+)", combined, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"\b(production|staging|qa|uat|dev|preprod|pre-prod|sandbox)\b", combined, re.IGNORECASE)
        return m.group(1) if m else None

    # Citations and evidence

    @staticmethod
    def _build_citations(context: ContextCollectionResult) -> list[dict[str, str]]:
        """Build citation list from all evidence sources."""
        c: list[dict[str, str]] = []
        for url in context.jenkins_links or []:
            c.append({"source": "Jenkins", "url": url, "type": "jenkins"})
        for s in context.rag_snippets or []:
            entry: dict[str, str] = {"source": s.source_title, "type": s.source_type, "excerpt": s.content[:200]}
            if s.source_url:
                entry["url"] = s.source_url
            c.append(entry)
        for e in (context.log_entries or [])[:5]:
            c.append({"source": f"Log ({e.source})", "excerpt": e.message[:200], "type": "log"})
        for tr in context.testrail_results or []:
            c.append({"source": f"TestRail: {tr.get('name', 'run')}", "url": tr.get("url", ""),
                       "excerpt": f"{tr.get('pass_rate', 0)}% pass, {tr.get('failed', 0)} failed",
                       "type": "testrail"})
        for pr in context.git_prs or []:
            sha = f" | merged commit {pr.merge_commit_sha}" if pr.merge_commit_sha else ""
            branch = f" | branch {pr.head_branch} → {pr.base_branch}" if pr.head_branch else ""
            c.append({"source": f"Git PR #{pr.pr_number} ({pr.provider})", "url": pr.pr_url,
                       "type": "git_pr", "excerpt": f"{pr.state} — {pr.pr_title}{sha}{branch}"})
        for e in (context.elk_log_entries or [])[:5]:
            cid = f" [{e.correlation_id}]" if e.correlation_id else ""
            c.append({"source": f"ELK log{cid}", "excerpt": e.message[:200], "type": "elk"})
        for tr in context.testrail_marker_results or []:
            c.append({"source": f"TestRail (marker={tr.get('marker', '?')}): {tr.get('name', 'run')}",
                       "url": tr.get("url", ""), "type": "testrail",
                       "excerpt": f"{tr.get('pass_rate', 0)}% pass, {tr.get('failed', 0)} failed (marker-filtered)"})
        for cc in context.confluence_citations or []:
            c.append({"source": cc.get("source", "Confluence"), "url": cc.get("url", ""),
                       "excerpt": cc.get("excerpt", "")[:200], "type": cc.get("type", "confluence")})
        if context.jenkins_test_report:
            rpt = context.jenkins_test_report
            c.append({"source": "Jenkins Test Report (JUnit)", "type": "jenkins",
                       "excerpt": (f"{rpt.get('passed', 0)}/{rpt.get('total', 0)} passed "
                                   f"({rpt.get('pass_rate', 0)}%), {rpt.get('failed', 0)} failed, "
                                   f"{rpt.get('errors', 0)} errors")})
        if context.jenkins_console_errors:
            errs = context.jenkins_console_errors
            c.append({"source": "Jenkins Console Errors", "type": "jenkins",
                       "excerpt": f"{errs.get('error_count', 0)} errors, "
                                  f"{errs.get('exception_count', 0)} exceptions detected"})
        for b in (context.jenkins_build_info or [])[:3]:
            sha = f" (commit {b.get('commit_sha', '')})" if b.get("commit_sha") else ""
            c.append({"source": f"Jenkins Build: {b.get('job_name', '')}", "url": b.get("url", ""),
                       "type": "jenkins",
                       "excerpt": f"#{b.get('build_number', '?')} — {b.get('result', '?')}{sha}"})
        for art in (context.s3_artifacts or [])[:5]:
            c.append({"source": f"S3 Artifact: {art.get('key', 'unknown')}", "url": art.get("url", ""),
                       "type": "s3",
                       "excerpt": f"Size: {art.get('size', 'N/A')} | Modified: {art.get('last_modified', 'N/A')}"})
        return c

    @staticmethod
    def _build_evidence_used(context: ContextCollectionResult) -> list[str]:
        """Build a human-readable list of evidence sources used."""
        ev: list[str] = []
        for url in context.jenkins_links or []:
            ev.append(f"Jenkins log: {url}")
        for s in context.rag_snippets or []:
            label = f"{s.source_type.title()}: {s.source_title}"
            if s.relevance_score >= 0.5:
                label += f" (relevance: {s.relevance_score:.0%})"
            ev.append(label)
        for e in (context.log_entries or [])[:5]:
            ev.append(f"Log ({e.source}): {e.correlation_id or e.source}")
        for tr in context.testrail_results or []:
            ev.append(f"TestRail run: {tr.get('name', 'unknown')} ({tr.get('pass_rate', 0)}% pass)")
        if context.build_metadata:
            bm = context.build_metadata
            parts = [p for p in [f"commit {bm['commit']}" if bm.get("commit") else None,
                                 bm.get("version")] if p]
            if parts:
                ev.append(f"Build: {', '.join(parts)}")
        for pr in context.git_prs or []:
            label = f"Git PR #{pr.pr_number} ({pr.state}): {pr.pr_title}"
            if pr.merge_commit_sha:
                label += f" — commit {pr.merge_commit_sha}"
            ev.append(label)
        for e in (context.elk_log_entries or [])[:5]:
            ev.append(f"ELK log ({e.level or 'INFO'}): {e.correlation_id or 'elk'}")
        for tr in context.testrail_marker_results or []:
            ev.append(f"TestRail (marker={tr.get('marker', '')}): {tr.get('name', 'run')} "
                       f"({tr.get('pass_rate', 0)}% pass, {tr.get('total', 0)} tests)")
        for cc in context.confluence_citations or []:
            ev.append(f"Confluence: {cc.get('source', 'page')}")
        if context.jenkins_test_report:
            rpt = context.jenkins_test_report
            ev.append(f"Jenkins JUnit: {rpt.get('passed', 0)}/{rpt.get('total', 0)} passed "
                       f"({rpt.get('pass_rate', 0)}%)")
        if context.jenkins_console_errors:
            errs = context.jenkins_console_errors
            ev.append(f"Jenkins console: {errs.get('error_count', 0)} errors, "
                       f"{errs.get('exception_count', 0)} exceptions")
        for b in (context.jenkins_build_info or [])[:3]:
            ev.append(f"Jenkins build #{b.get('build_number', '?')}: {b.get('result', '?')}")
        if context.pipeline_correlation:
            pc = context.pipeline_correlation
            ev.append(f"Pipeline correlation: {pc.get('jenkins_build_count', 0)} builds, "
                       f"{pc.get('testrail_run_count', 0)} runs, "
                       f"{pc.get('confluence_citation_count', 0)} docs")
        for art in (context.s3_artifacts or [])[:5]:
            ev.append(f"S3 artifact: {art.get('key', 'unknown')}")
        return ev

    # Suggested labels

    @staticmethod
    def _suggest_actions(classification: CommentClassification) -> list[dict[str, str]]:
        """Suggest Jira actions based on classification."""
        return list(_ACTION_MAP.get(classification.comment_type, []))

    @staticmethod
    def _suggest_labels(classification: CommentClassification) -> list[str]:
        """Suggest labels based on classification."""
        labels: list[str] = []
        if classification.missing_context:
            labels.append("needs-info")
        mapped = _LABEL_MAP.get(classification.comment_type)
        if mapped:
            labels.append(mapped)
        return labels
