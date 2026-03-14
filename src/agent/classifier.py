"""Comment classifier – determines comment intent."""

from __future__ import annotations
import json
import logging
from typing import Optional

from src.models.comment import Comment
from src.models.classification import CommentClassification, CommentType

logger = logging.getLogger(__name__)

# Keyword rules are a fallback when LLM classification is unavailable or low-confidence.

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
    (
        ["duplicate", "already fixed", "fixed in previous", "same as",
         "duplicate of", "dup of", "already reported", "known issue"],
        CommentType.DUPLICATE_FIXED,
        "Comment indicates this is a duplicate or already fixed in another ticket",
        [],
    ),
    (
        ["blocked by", "waiting for", "depends on", "dependency",
         "blocked on", "waiting on", "upstream", "pending"],
        CommentType.BLOCKED_WAITING,
        "Comment indicates work is blocked by a dependency or waiting for external input",
        ["Blocking issue key", "Expected resolution timeline"],
    ),
    (
        ["configuration issue", "config issue", "misconfigured", "misconfiguration",
         "not a bug", "setup issue", "wrong config", "config error",
         "environment setup", "user error"],
        CommentType.CONFIG_ISSUE,
        "Comment suggests this is a configuration or setup issue, not a code defect",
        ["Correct configuration steps", "Documentation reference"],
    ),
]

# Copilot SDK classification prompt

_COPILOT_SYSTEM_PROMPT = """\
You are a Jira comment classifier for a QA team. Given a developer comment on a
bug ticket, classify it into exactly ONE of these categories:

  cannot_reproduce    – Developer says they cannot reproduce the issue
  need_more_info      – Comment requests logs, environment details, or other info
  fixed_validate      – A fix is ready and needs validation / retesting
  by_design           – Behavior is by design / expected / as specified
  duplicate_fixed     – Issue is a duplicate or was already fixed in another ticket
  blocked_waiting     – Work is blocked by a dependency or waiting for something
  config_issue        – Not a code bug; it’s a configuration / setup issue
  other               – Does not clearly fit any of the above

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

    def __init__(self, llm_client=None, api_key: Optional[str] = None, model: str = "gpt-4"):
        """
        Args:
            llm_client: :class:`~src.llm.client.CopilotLLMClient` instance.
                        If *None*, the module-level singleton is used.
                        Pass a disabled client for keyword-only mode (CI / tests).
            api_key:    Deprecated — kept for backward compatibility only.
            model:      Deprecated — kept for backward compatibility only.
        """
        if llm_client is None:
            from src.llm.client import get_llm_client
            llm_client = get_llm_client()
        self._llm = llm_client
        if self._llm.enabled:
            logger.info(
                "Copilot SDK classifier initialised (backend=%s)", self._llm.backend
            )
        else:
            logger.info("Copilot LLM not available — using keyword-only classification")

    # Public API
    def classify(self, comment: Comment) -> CommentClassification:
        """Classify *comment* using Copilot SDK → keyword fallback chain."""

        # 1. Copilot LLM
        if self._llm.enabled:
            sdk_result = self._classify_with_copilot(comment)
            if sdk_result is not None and sdk_result.confidence >= 0.6:
                return sdk_result
            logger.info(
                "Copilot LLM classification low-confidence (%.2f) – falling back to keywords",
                sdk_result.confidence if sdk_result else 0,
            )
        # 2. Keyword fallback
        return self._classify_with_keywords(comment)

    # Copilot LLM path
    def _classify_with_copilot(self, comment: Comment) -> Optional[CommentClassification]:
        """Call Copilot LLM and parse structured JSON output."""
        try:
            text = self._llm.complete(
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
            if not text:
                return None
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
            logger.warning("Copilot LLM classification failed: %s", exc)
            return None

    # Keyword path
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

    # Helpers
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
            CommentType.DUPLICATE_FIXED: [
                "Which ticket is this a duplicate of?",
                "Which version/build contains the fix?",
            ],
            CommentType.BLOCKED_WAITING: [
                "Which issue/dependency is blocking this?",
                "What is the expected timeline for resolution?",
            ],
            CommentType.CONFIG_ISSUE: [
                "What is the correct configuration?",
                "Is there documentation for the expected setup?",
            ],
        }
        return mapping.get(ctype)
