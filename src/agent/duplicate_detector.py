"""Duplicate detector — finds similar past drafts on the same issue."""


from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage.sqlite_store import SQLiteDraftStore

logger = logging.getLogger(__name__)

# Minimum Jaccard similarity (0–1) to treat as a potential duplicate
_DEFAULT_THRESHOLD = 0.25

# Max characters shown as body preview in the UI
_PREVIEW_LEN = 120


@dataclass
class SimilarDraft:
    """A past draft that is semantically similar to the current comment."""
    draft_id: str
    issue_key: str
    status: str
    similarity: float          # Jaccard score 0–1
    body_preview: str          # first _PREVIEW_LEN chars of draft body
    created_at: str            # ISO-8601, truncated to seconds


@dataclass
class DuplicateCheckResult:
    """Result of a duplicate check for one incoming comment."""
    similar_drafts: list[SimilarDraft] = field(default_factory=list)

    @property
    def is_likely_duplicate(self) -> bool:
        return bool(self.similar_drafts)

    def to_dict_list(self) -> list[dict]:
        """Serialise to a list of plain dicts for storage in the Draft model."""
        return [
            {
                "draft_id": s.draft_id,
                "issue_key": s.issue_key,
                "status": s.status,
                "similarity": s.similarity,
                "body_preview": s.body_preview,
                "created_at": s.created_at,
            }
            for s in self.similar_drafts
        ]

def _tokenize(text: str) -> set[str]:
    """Return a set of lowercase word tokens (min 3 chars, letters only)."""
    return set(re.findall(r"\b[a-z]{3,}\b", text.lower()))


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

class DuplicateDetector:
    """Check whether a new comment looks like a question already answered."""

    def __init__(self, threshold: float = _DEFAULT_THRESHOLD) -> None:
        self._threshold = threshold

    def check(
        self,
        comment_body: str,
        issue_key: str,
        draft_store: "SQLiteDraftStore",
        limit: int = 3,
    ) -> DuplicateCheckResult:
        """Return past drafts on *issue_key* that overlap with *comment_body*. """
        past = draft_store.find_recent_by_issue(issue_key, limit=20)
        if not past:
            return DuplicateCheckResult()

        comment_tokens = _tokenize(comment_body)
        hits: list[SimilarDraft] = []

        for d in past:
            # Prefer comparing against the original triggering comment
            # (comment-to-comment is much more accurate than comment-to-draft
            # because draft bodies are long templates that dilute Jaccard scores).
            compare_text = d.get("trigger_comment_body") or d.get("body", "")
            sim = _jaccard(comment_tokens, _tokenize(compare_text))
            if sim >= self._threshold:
                body = d.get("body", "")
                hits.append(
                    SimilarDraft(
                        draft_id=d.get("draft_id", ""),
                        issue_key=d.get("issue_key", issue_key),
                        status=d.get("status", "unknown"),
                        similarity=round(sim, 3),
                        body_preview=body[:_PREVIEW_LEN].replace("\n", " "),
                        created_at=(d.get("created_at") or "")[:19],
                    )
                )

        hits.sort(key=lambda x: x.similarity, reverse=True)
        top = hits[:limit]

        if top:
            logger.info(
                "Duplicate check %s: %d similar draft(s) found (top=%.2f)",
                issue_key,
                len(top),
                top[0].similarity,
            )

        return DuplicateCheckResult(similar_drafts=top)
