"""Comment classification module using Copilot SDK with keyword fallback."""

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
]

_COPILOT_SYSTEM_PROMPT = """\
Classify this Jira comment into ONE of these categories:
  cannot_reproduce  – developer cannot reproduce the issue
  need_more_info    – more logs/details/steps needed
  fixed_validate    – fix is ready and needs validation
  by_design         – behaviour is intentional/expected
  other             – does not fit any of the above

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
    ):
        """Initialize classifier.

        Args:
            api_key: GitHub Copilot SDK API key.
            model: claude-sonnet-4.5.
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
                logger.info("Copilot SDK initialized (model=%s)", model)
            except ImportError:
                logger.warning("copilot SDK not available; using keyword fallback")
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            logger.info(
                "Local LLM provider enabled for classifier (provider=%s, base=%s)",
                self._provider,
                self._base_url,
            )

    async def classify(self, comment: Comment) -> CommentClassification:
        """Classify a comment into a predefined type.

        Args:
            comment: The comment to classify.

        Returns:
            Classification with type and confidence score.
        """
        if self._provider == "copilot" and self._client:
            result = await self._classify_with_copilot(comment)
            if result and result.confidence >= 0.6:
                return result
        elif self._provider in {"llama_cpp", "local", "openai_compat"}:
            result = await self._classify_with_local_llm(comment)
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
                            "content": f'Issue {comment.issue_key}:\n"""\n{comment.body}\n"""',
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
        loop = asyncio.get_event_loop()

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
        }
        return mapping.get(ctype)
