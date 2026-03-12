"""Comment classifier – determines comment intent.

Strategy:
  1. Try Copilot SDK for structured classification.
  2. Fall back to keyword heuristics if the Copilot SDK is unavailable or low-confidence.

Classification buckets:
  • Cannot Repro
  • Need Info / Logs
  • Fixed — Validate
  • By Design
  • Duplicate / Already Fixed
  • Blocked / Waiting
  • Configuration Issue
  • Other (fallback)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import requests

from src.models.classification import CommentClassification, CommentType
from src.models.comment import Comment

logger = logging.getLogger(__name__)

# Keyword-based classification fallback rules
_KEYWORD_RULES = [
    (
        (
            "can't repro",
            "cannot repro",
            "cannot reproduce",
            "failed to reproduce",
            "unable to reproduce",
            "not reproducible",
            "works on my machine",
            "works for me",
        ),
        CommentType.CANNOT_REPRODUCE,
        "Developer cannot reproduce the issue.",
        ["environment", "browser", "os", "steps"],
    ),
    (
        (
            "need more info",
            "need info",
            "missing context",
            "unclear",
            "logs",
            "provide",
            "trace",
            "correlation",
        ),
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

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4.5",
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        github_token: Optional[str] = None,
    ):
        """Initialize classifier.

        Args:
            api_key: Deprecated – use github_token instead.
            model: Model name for Copilot SDK sessions.
            github_token: GitHub token for Copilot SDK auth.
        """
        self._client = None
        self._model = model
        self._provider = (provider or os.getenv("LLM_PROVIDER", "copilot")).lower()
        self._base_url = (base_url or os.getenv("LLM_BASE_URL", "http://localhost:8080")).rstrip(
            "/"
        )
        self._llm_api_key = llm_api_key or os.getenv("LLM_API_KEY", "")

        token = github_token or api_key
        if self._provider == "copilot" and token:
            try:
                from copilot import CopilotClient

                self._client = CopilotClient({"github_token": token})
                logger.info("Copilot SDK initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using keyword fallback")
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            logger.info(
                "Local LLM provider enabled for classifier (provider=%s, base=%s)",
                self._provider,
                self._base_url,
            )

    async def classify(
        self, comment: Comment, *, context=None,
    ) -> CommentClassification:
        """Classify a comment into a predefined type.

        Args:
            comment: The comment to classify.
            context: Optional ContextCollectionResult for richer classification.

        Returns:
            Classification with type and confidence score.
        """
        # Build enriched body: comment + issue description + recent comments
        enriched_body = comment.body
        if context and context.issue_context:
            issue = context.issue_context
            parts = [comment.body]
            if issue.description:
                parts.append(issue.description)
            if issue.last_comments:
                for c in issue.last_comments[-3:]:
                    if c.body:
                        parts.append(c.body)
            enriched_body = "\n---\n".join(parts)

        enriched_comment = Comment(
            comment_id=comment.comment_id,
            issue_key=comment.issue_key,
            author=comment.author,
            created=comment.created,
            updated=comment.updated,
            body=enriched_body,
        )
        if self._provider == "copilot" and self._client:
            result = await self._classify_with_copilot(enriched_comment)
            if result and result.confidence >= 0.6:
                return result
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            result = await self._classify_with_local_llm(enriched_comment)
            if result and result.confidence >= 0.6:
                return result

        return self._classify_with_keywords(comment)

    async def _classify_with_copilot(self, comment: Comment) -> Optional[CommentClassification]:
        """Classify using Copilot SDK session."""
        session = None
        try:
            session = await self._client.create_session({
                "model": self._model,
                "available_tools": [],
                "system_message": {
                    "mode": "replace",
                    "content": _COPILOT_SYSTEM_PROMPT,
                },
            })

            response = await session.send_and_wait({
                "prompt": f'Issue {comment.issue_key}:\n"""\n{comment.body}\n"""',
            })

            if not response or not response.data or not response.data.content:
                return None

            text = response.data.content.strip()
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
        finally:
            if session:
                try:
                    await session.disconnect()
                except Exception:
                    pass

    async def _classify_with_local_llm(self, comment: Comment) -> Optional[CommentClassification]:
        """Classify using a local/OpenAI-compatible endpoint (e.g., llama.cpp server)."""
        try:
            text = await self._chat_completion_text(
                messages=[
                    {"role": "system", "content": _COPILOT_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f'Issue {comment.issue_key}:\n"""\n{comment.body}\n"""',
                    },
                ],
                max_tokens=256,
                temperature=0.1,
            )
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
            logger.warning("Local LLM classification failed: %s", e)
            return None

    async def _chat_completion_text(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Call OpenAI-compatible chat completions endpoint and return message text."""
        loop = asyncio.get_running_loop()

        def _do_request() -> str:
            headers = {"Content-Type": "application/json"}
            if self._llm_api_key:
                headers["Authorization"] = f"Bearer {self._llm_api_key}"

            resp = requests.post(
                f"{self._base_url}/v1/chat/completions",
                json={
                    "model": self._model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            return payload["choices"][0]["message"]["content"].strip()

        return await loop.run_in_executor(None, _do_request)

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
