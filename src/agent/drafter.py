"""Draft generator – creates responses using templates + Copilot SDK."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType
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

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4.5"):
        """Initialize drafter.
        
        Args:
            api_key: GitHub Copilot SDK API key (optional).
            model: LLM model to use (default: claude-sonnet-4.5).
        """
        self._client = None
        self._model = model
        if api_key:
            try:
                from copilot import CopilotClient
                self._client = CopilotClient()
                logger.info("Copilot SDK drafter initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using template-only mode")

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
        template = TEMPLATES.get(
            classification.comment_type,
            TEMPLATES[CommentType.OTHER]
        )
        
        # Build context dict for template filling
        issue = context.issue_context if context else None
        context_dict = {
            "issue_key": comment.issue_key,
            "summary": issue.summary if issue else "",
            "environment": issue.environment if issue else "staging",
            "build_version": "latest",
            "observation": "Issue reproduced",
            "repro_steps": "1. See issue description",
            "feature_flag": "N/A",
            "existing_evidence": "None",
            "missing_items": [],
            "component": issue.components[0] if issue and issue.components else "system",
            "time_window": "last 24h",
            "doc_link": "",
            "expected_behavior": "",
            "fix_version": "",
            "retest_checklist": "",
            "target_env": "production",
        }
        
        # Fill template
        content = template.format(**context_dict)
        
        # Optionally refine with Copilot
        if self._client:
            refined = await self._refine_with_copilot(content)
            if refined:
                content = refined
        
        return Draft(
            draft_id=f"draft_{int(datetime.now(timezone.utc).timestamp())}",
            issue_key=comment.issue_key,
            in_reply_to_comment_id=comment.comment_id,
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body=content,
            status=DraftStatus.GENERATED,
            citations=[],
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
