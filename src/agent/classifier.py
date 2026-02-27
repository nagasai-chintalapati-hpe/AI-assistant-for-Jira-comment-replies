"""Comment classifier – determines comment intent.

MVP v1 strategy:
  1. Try Copilot SDK for structured classification.
  2. Fall back to keyword heuristics if the Copilot SDK is unavailable or low-confidence.

Classification buckets (MVP v1):
  • Cannot Repro
  • Need Info / Logs
  • Fixed — Validate
  • By Design
  • Other (fallback)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType

logger = logging.getLogger(__name__)

# ---- keyword rules (fallback) ----------------------------------------- #

_KEYWORD_RULES: list[tuple[list[str], CommentType, str, list[str]]] = [
    # (keywords, type, reasoning, missing_context)
    (
        ["cannot reproduce", "can't reproduce", "cannot repro", "can't repro",
         "unable to reproduce", "not reproducible", "works on my machine"],
        CommentType.CANNOT_REPRODUCE,
        "Developer indicates inability to reproduce the issue",
        ["Environment details", "Reproduction steps", "Browser/OS version"],
    ),
    (
        ["need logs", "need more info", "provide logs", "attach logs",
         "error log", "stack trace", "log file", "share logs",
         "need environment", "need details"],
        CommentType.NEED_MORE_INFO,
        "Comment requests logs or diagnostic information",
        ["Log attachments", "Error messages", "Correlation IDs"],
    ),
    (
        ["fix ready", "fix deployed", "fix released", "fix available",
         "please validate", "please verify", "ready for testing",
         "fixed in", "fix merged", "already fixed", "fixed in build",
         "resolved in"],
        CommentType.FIXED_VALIDATE,
        "Developer indicates a fix is ready for validation",
        [],
    ),
    (
        ["as designed", "by design", "expected behavior", "expected behaviour",
         "working as intended", "not a defect"],
        CommentType.BY_DESIGN,
        "Comment suggests this is expected / by-design behavior",
        [],
    ),
]

# ---- Copilot SDK classification prompt --------------------------------- #

_COPILOT_SYSTEM_PROMPT = """\
You are a Jira comment classifier for a QA team. Given a developer comment on a
bug ticket, classify it into exactly ONE of these categories:

  cannot_reproduce, need_more_info, fixed_validate, by_design, other

Respond ONLY with valid JSON – no markdown, no explanation:
{
  "comment_type": "<category>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>",
  "missing_context": ["<item>", ...],
  "suggested_questions": ["<question>", ...]
}
"""


class CommentClassifier:
    """Classifies developer comments into predefined types.

    Attempts Copilot SDK-based classification first; falls back to keyword rules.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4"):
        """
        Args:
            api_key: Copilot SDK API key.  If *None* the classifier
                     operates in keyword-only mode (safe for CI / tests).
            model:   Model name to use via Copilot SDK.
        """
        self._client = None
        self._model = model
        if api_key:
            try:
                from openai import OpenAI  # Copilot SDK compatible client

                self._client = OpenAI(api_key=api_key)
                logger.info("Copilot SDK classifier initialised (model=%s)", model)
            except Exception as exc:
                logger.warning("Could not initialise Copilot SDK (%s) – using keyword fallback", exc)

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def classify(self, comment: Comment) -> CommentClassification:
        """Classify *comment* using Copilot SDK → keyword fallback chain."""

        # 1. Try Copilot SDK
        if self._client is not None:
            sdk_result = self._classify_with_copilot(comment)
            if sdk_result is not None and sdk_result.confidence >= 0.6:
                return sdk_result
            logger.info(
                "Copilot SDK classification low-confidence (%.2f) – falling back to keywords",
                sdk_result.confidence if sdk_result else 0,
            )

        # 2. Keyword fallback
        return self._classify_with_keywords(comment)

    # ------------------------------------------------------------------ #
    #  Copilot SDK path                                                   #
    # ------------------------------------------------------------------ #

    def _classify_with_copilot(self, comment: Comment) -> Optional[CommentClassification]:
        """Call Copilot SDK and parse structured output."""
        try:
            response = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self._model,
                messages=[
                    {"role": "system", "content": _COPILOT_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Comment on {comment.issue_key}:\n"
                            f'"""\n{comment.body}\n"""'
                        ),
                    },
                ],
                max_tokens=256,
                temperature=0.1,
            )
            text = response.choices[0].message.content.strip()
            data = json.loads(text)

            ctype = CommentType(data["comment_type"])
            return CommentClassification(
                comment_id=comment.comment_id,
                comment_type=ctype,
                confidence=float(data.get("confidence", 0.7)),
                reasoning=data.get("reasoning", ""),
                missing_context=data.get("missing_context"),
                suggested_questions=data.get("suggested_questions"),
            )
        except Exception as exc:
            logger.warning("Copilot SDK classification failed: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    #  Keyword path                                                       #
    # ------------------------------------------------------------------ #

    def _classify_with_keywords(self, comment: Comment) -> CommentClassification:
        """Rule-based classification via keyword matching."""
        body_lower = comment.body.lower()

        for keywords, ctype, reasoning, missing in _KEYWORD_RULES:
            if any(kw in body_lower for kw in keywords):
                return CommentClassification(
                    comment_id=comment.comment_id,
                    comment_type=ctype,
                    confidence=0.85,
                    reasoning=reasoning,
                    missing_context=missing or None,
                    suggested_questions=self._default_questions(ctype),
                )

        # No match
        return CommentClassification(
            comment_id=comment.comment_id,
            comment_type=CommentType.OTHER,
            confidence=0.50,
            reasoning="Comment type could not be determined with high confidence",
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_questions(ctype: CommentType) -> Optional[list[str]]:
        """Return sensible follow-up questions per classification."""
        mapping: dict[CommentType, list[str]] = {
            CommentType.CANNOT_REPRODUCE: [
                "What is your environment (OS, browser version)?",
                "Can you provide step-by-step reproduction steps?",
                "Which build/version did you test on?",
            ],
            CommentType.NEED_MORE_INFO: [
                "Can you attach the relevant log files?",
                "What is the correlation ID or request ID?",
            ],
            CommentType.FIXED_VALIDATE: [
                "Which build/version contains the fix?",
                "What are the focused retest steps?",
            ],
        }
        return mapping.get(ctype)
