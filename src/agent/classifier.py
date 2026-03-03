"""Comment classification module using Copilot SDK with keyword fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType

logger = logging.getLogger(__name__)

# Keyword-based classification fallback rules
_KEYWORD_RULES = [
    (
        ("can't repro", "cannot repro", "cannot reproduce", "failed to reproduce", "unable to reproduce"),
        CommentType.CANNOT_REPRODUCE,
        "Developer cannot reproduce the issue.",
        ["environment", "browser", "os", "steps"],
    ),
    (
        ("need more info", "need info", "missing context", "unclear", "logs", "provide", "trace", "correlation"),
        CommentType.NEED_MORE_INFO,
        "More information needed from reporter.",
        ["logs", "trace", "details"],
    ),
    (
        ("fixed", "released", "deployed", "merged"),
        CommentType.FIXED_VALIDATE,
        "Issue appears fixed; validation requested.",
        ["build", "version", "release"],
    ),
    (
        ("by design", "expected", "working as intended", "not a bug"),
        CommentType.BY_DESIGN,
        "Issue is by design, not a bug.",
        [],
    ),
]

_COPILOT_SYSTEM_PROMPT = """\
Classify this Jira comment into ONE category: cannot_reproduce, need_more_info, \
fixed_validate, by_design, duplicate, not_a_bug, blocked, status_update, or other.

Respond ONLY with valid JSON (no markdown):
{
  "comment_type": "<category>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>",
  "missing_context": ["<item>"],
  "suggested_questions": ["<question>"]
}
"""


class CommentClassifier:
    """Classifies developer comments using Copilot SDK or keyword fallback."""

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4.5"):
        """Initialize classifier.
        
        Args:
            api_key: GitHub Copilot SDK API key.
            model: claude-sonnet-4.5.
        """
        self._client = None
        self._model = model
        if api_key:
            try:
                from copilot import CopilotClient
                self._client = CopilotClient()
                logger.info("Copilot SDK initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using keyword fallback")

    async def classify(self, comment: Comment) -> CommentClassification:
        """Classify a comment into a predefined type.
        
        Args:
            comment: The comment to classify.
            
        Returns:
            Classification with type and confidence score.
        """
        if self._client:
            result = await self._classify_with_copilot(comment)
            if result and result.confidence >= 0.6:
                return result
        
        return self._classify_with_keywords(comment)

    async def _classify_with_copilot(self, comment: Comment) -> Optional[CommentClassification]:
        """Classify using Copilot SDK."""
        try:
            # Create session for Copilot interaction
            loop = asyncio.get_event_loop()
            
            response = await loop.run_in_executor(
                None,
                lambda: self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _COPILOT_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": f"Issue {comment.issue_key}:\n\"\"\"\n{comment.body}\n\"\"\"",
                        },
                    ],
                    max_tokens=256,
                    temperature=0.1,
                ),
            )
            
            text = response.choices[0].message.content.strip()
            data = json.loads(text)

            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=CommentType(data["comment_type"]),
                confidence=float(data.get("confidence", 0.7)),
                reasoning=data.get("reasoning", ""),
                missing_context=data.get("missing_context"),
                suggested_questions=data.get("suggested_questions"),
            )
        except Exception as e:
            logger.warning("Copilot classification failed: %s", e)
            return None

    def _classify_with_keywords(self, comment: Comment) -> CommentClassification:
        """Classify using keyword rules."""
        body_lower = comment.body.lower()

        for keywords, ctype, reasoning, missing in _KEYWORD_RULES:
            if any(kw in body_lower for kw in keywords):
                return CommentClassification(
                    comment_id=comment.comment_id,
                    comment_type=ctype,
                    confidence=0.85,
                    reasoning=reasoning,
                    missing_context=missing or None,
                    suggested_questions=self._get_default_questions(ctype),
                )

        return CommentClassification(
            comment_id=comment.comment_id,
            comment_type=CommentType.OTHER,
            confidence=0.50,
            reasoning="Could not determine type with confidence.",
        )

    @staticmethod
    def _get_default_questions(ctype: CommentType) -> Optional[list[str]]:
        """Return suggested follow-up questions."""
        mapping = {
            CommentType.CANNOT_REPRODUCE: [
                "What is your environment (OS, browser)?",
                "Can you provide step-by-step reproduction?",
                "Which build/version did you test on?",
            ],
            CommentType.NEED_MORE_INFO: [
                "Can you attach the log files?",
                "What is the correlation ID?",
            ],
            CommentType.FIXED_VALIDATE: [
                "Which build contains the fix?",
                "What are the retest steps?",
            ],
        }
        return mapping.get(ctype)
