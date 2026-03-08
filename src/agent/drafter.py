"""Draft generator – creates responses using templates + Copilot SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests

from src.models.classification import CommentClassification, CommentType
from src.models.comment import Comment
from src.models.context import ContextCollectionResult
from src.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

# Response templates per classification type
TEMPLATES: dict[CommentType, str] = {
    CommentType.CANNOT_REPRODUCE: (
        "Thanks for the update. We tried on **{environment}** (Build {build_version}).\n\n"
        "**Observed:** {observation}\n\n"
        "**Steps:**\n{repro_steps}\n\n"
        "Could you confirm:\n"
        "• Your environment (OS, browser)?\n"
        "• Which build/version did you test on?\n"
        "• Feature flag `{feature_flag}` enabled?\n\n"
        "Please retest on the latest staging build."
    ),
    CommentType.NEED_MORE_INFO: (
        "Thanks for flagging this. We have:\n"
        "{existing_evidence}\n\n"
        "We need:\n{missing_items}\n\n"
        "Please provide:\n"
        "• Exact repro steps + correlation IDs\n"
        "• **{component}** logs for **{time_window}**\n\n"
        "This will help us identify the root cause."
    ),
    CommentType.BY_DESIGN: (
        "Thanks for raising this. This is **expected behavior**.\n\n"
        "**Reference:** {doc_link}\n\n"
        "Per specification:\n> {expected_behavior}\n\n"
        "If this doesn't match acceptance criteria, please point us to the "
        "specific requirement so we can assess doc updates."
    ),
    CommentType.FIXED_VALIDATE: (
        "A fix has been deployed.\n\n"
        "**Version/Build:** {fix_version}\n\n"
        "**Retest checklist:**\n{retest_checklist}\n\n"
        "Please verify in **{target_env}** and update the ticket status."
    ),
    CommentType.OTHER: (
        "Thank you for your comment. We're reviewing this and will "
        "follow up shortly.\n\n"
        "**Issue:** {issue_key} – {summary}"
    ),
}

_REFINE_SYSTEM = """\
You are a QA engineer writing a reply on a Jira defect.
Rewrite the draft below to be professional, concise, and empathetic.
Keep all factual data (build numbers, links) intact. Do NOT invent facts.
Output ONLY the refined text – no markdown or explanation.
"""


class ResponseDrafter:
    """Generates draft responses using templates + optional Copilot SDK refinement."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4.5",
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
    ):
        """Initialize drafter.

        Args:
            api_key: GitHub Copilot SDK API key (optional).
            model: LLM model to use (default: claude-sonnet-4.5).
        """
        self._client = None
        self._model = model
        self._provider = (provider or os.getenv("LLM_PROVIDER", "copilot")).lower()
        self._base_url = (base_url or os.getenv("LLM_BASE_URL", "http://localhost:8080")).rstrip(
            "/"
        )
        self._llm_api_key = llm_api_key or os.getenv("LLM_API_KEY", "")

        if self._provider == "copilot" and api_key:
            try:
                from copilot import CopilotClient

                self._client = CopilotClient()
                logger.info("Copilot SDK drafter initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using template-only mode")
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            logger.info(
                "Local LLM provider enabled for drafter (provider=%s, base=%s)",
                self._provider,
                self._base_url,
            )

    async def draft(
        self,
        comment: Comment,
        classification: CommentClassification,
        context: ContextCollectionResult,
    ) -> Draft:
        """Generate a draft response.

        Args:
            comment: The original comment.
            classification: Classification result.
            context: Collected context (issue fields, evidence).

        Returns:
            Draft with suggested content and citations.
        """
        # Get template for this classification type
        template = TEMPLATES.get(classification.comment_type, TEMPLATES[CommentType.OTHER])

        # Build context dict from real issue data
        context_dict = self._build_context_dict(comment, classification, context)

        # Fill template
        content = template.format(**context_dict)

        # Build citations from real evidence
        citations = self._build_citations(context)

        # Optionally refine with Copilot
        if self._provider == "copilot" and self._client:
            refined = await self._refine_with_copilot(content)
            if refined:
                content = refined
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            refined = await self._refine_with_local_llm(content)
            if refined:
                content = refined

        return Draft(
            draft_id=f"draft_{uuid.uuid4().hex[:12]}",
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

    async def _refine_with_copilot(self, draft_text: str) -> Optional[str]:
        """Refine draft using Copilot SDK."""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _REFINE_SYSTEM},
                        {"role": "user", "content": draft_text},
                    ],
                    max_tokens=512,
                    temperature=0.3,
                ),
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("Copilot refinement failed: %s", e)
            return None

    async def _refine_with_local_llm(self, draft_text: str) -> Optional[str]:
        """Refine draft using a local/OpenAI-compatible endpoint (e.g., llama.cpp server)."""
        try:
            loop = asyncio.get_event_loop()

            def _do_request() -> str:
                headers = {"Content-Type": "application/json"}
                if self._llm_api_key:
                    headers["Authorization"] = f"Bearer {self._llm_api_key}"

                resp = requests.post(
                    f"{self._base_url}/v1/chat/completions",
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
    def _build_context_dict(
        comment: Comment,
        classification: CommentClassification,
        context: ContextCollectionResult,
    ) -> dict:
        """Extract real values from collected context for template filling."""
        issue = context.issue_context if context else None

        # Build version from fix versions or latest changelog
        versions = issue.versions if issue and issue.versions else []
        build_version = versions[0] if versions else "latest"

        # Repro steps from description
        description = (issue.description if issue else "") or ""
        repro_steps = description[:500] if description else "See issue description"

        # Existing evidence summary
        evidence_parts: list[str] = []
        if issue and issue.attached_files:
            filenames = [a.get("filename", "") for a in issue.attached_files[:5]]
            evidence_parts.append(f"Attachments: {', '.join(filenames)}")
        if issue and issue.last_comments:
            evidence_parts.append(f"{len(issue.last_comments)} recent comment(s)")
        existing_evidence = (
            "\n".join(f"• {e}" for e in evidence_parts) or "No evidence collected yet"
        )

        # Missing items from classification
        missing_raw = classification.missing_context or []
        missing_items = "\n".join(f"• {m}" for m in missing_raw) or "• Additional details"

        # Component
        component = issue.components[0] if issue and issue.components else "system"

        # Fix version / retest checklist
        fix_versions = [v for v in versions if v]
        fix_version = fix_versions[0] if fix_versions else "pending"

        # Retest checklist from changelog transitions
        checklist_items: list[str] = []
        if issue and issue.changelog:
            for entry in issue.changelog[-3:]:
                for item in entry.get("items", []):
                    if item.get("field") == "status":
                        checklist_items.append(
                            f"- Verify transition: {item.get('from', '?')} → {item.get('to', '?')}"
                        )
        checklist_items.append("- Verify fix in target environment")
        checklist_items.append("- Confirm no regressions")
        retest_checklist = "\n".join(checklist_items)

        return {
            "issue_key": comment.issue_key,
            "summary": issue.summary if issue else "",
            "environment": (issue.environment if issue and issue.environment else "staging"),
            "build_version": build_version,
            "observation": description[:200] if description else "Observed issue as reported",
            "repro_steps": repro_steps,
            "feature_flag": "N/A",
            "existing_evidence": existing_evidence,
            "missing_items": missing_items,
            "component": component,
            "time_window": "last 24h",
            "doc_link": "(see issue description)",
            "expected_behavior": description[:300] if description else "As documented",
            "fix_version": fix_version,
            "retest_checklist": retest_checklist,
            "target_env": (issue.environment if issue and issue.environment else "staging"),
        }

    @staticmethod
    def _build_citations(context: ContextCollectionResult) -> list[dict[str, str]]:
        """Build citations list from real evidence sources."""
        citations: list[dict[str, str]] = []
        issue = context.issue_context if context else None

        if issue and issue.attached_files:
            for att in issue.attached_files[:5]:
                citations.append(
                    {
                        "source": f"Attachment: {att.get('filename', 'unknown')}",
                        "url": att.get("content_url", ""),
                        "excerpt": f"{att.get('mime_type', '')} ({att.get('size', 0)} bytes)",
                    }
                )

        if context and context.jenkins_links:
            snippets = context.jenkins_log_snippets or {}
            for link in context.jenkins_links[:3]:
                snippet = snippets.get(link, "")
                excerpt = snippet[-500:] if snippet else "Console output from CI build"
                citations.append(
                    {
                        "source": "Jenkins Build Log",
                        "url": link,
                        "excerpt": excerpt,
                    }
                )

        if issue and issue.linked_issues:
            for li in issue.linked_issues[:3]:
                citations.append(
                    {
                        "source": f"Linked Issue: {li.get('key', '')}",
                        "url": "",
                        "excerpt": f"{li.get('type', '')} – {li.get('status', '')}",
                    }
                )

        return citations

    @staticmethod
    def _suggest_actions(classification: CommentClassification) -> list[str]:
        """Suggest next actions based on classification."""
        mapping = {
            CommentType.CANNOT_REPRODUCE: ["Request environment details", "Share repro steps"],
            CommentType.NEED_MORE_INFO: ["Request logs", "Provide correlation ID"],
            CommentType.FIXED_VALIDATE: ["Create retest checklist", "Deploy to staging"],
            CommentType.BY_DESIGN: ["Review acceptance criteria", "Update documentation"],
        }
        return mapping.get(classification.comment_type, [])
