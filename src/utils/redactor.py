"""PII and secret redactor.

Scrubs sensitive patterns from text *before* sending to the Copilot LLM
or persisting in the audit log.  Applied to:

  • Jira issue descriptions and comment bodies
  • Log excerpts from Jenkins / ELK
  • Any free-text field passed through the pipeline

Patterns masked
---------------
  Bearer / Authorization tokens  →  ``[REDACTED]``
  Generic ``key=value`` secrets  →  ``[REDACTED]``
  Basic-auth in URLs             →  ``[USER]:[REDACTED]@``
  Email addresses                →  ``[EMAIL]``
  Private IPv4 ranges            →  ``[PRIVATE_IP]``
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------------------ #
# Compiled patterns (ordered — most specific first)                   #
# ------------------------------------------------------------------ #

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Authorization: Bearer <token>
    (
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"),
        r"\1[REDACTED]",
    ),
    # Generic key=value / key: value secrets
    (
        re.compile(
            r"(?i)"
            r"(password|passwd|pwd|secret|api[_\-]?key|auth[_\-]?token"
            r"|access[_\-]?token|private[_\-]?key|client[_\-]?secret)"
            r"\s*[=:]\s*\S+"
        ),
        r"\1=[REDACTED]",
    ),
    # Inline Basic-auth in URLs:  https://user:pass@host
    (
        re.compile(r"(?i)(https?://)([^:/@\s]+):([^@\s]+)@"),
        r"\1[USER]:[REDACTED]@",
    ),
    # Email addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[EMAIL]",
    ),
    # Private IPv4 ranges  (10.x, 172.16-31.x, 192.168.x)
    (
        re.compile(
            r"\b(?:"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r")\b"
        ),
        "[PRIVATE_IP]",
    ),
]


@dataclass
class RedactionResult:
    """Outcome of a redaction pass."""

    original_length: int
    redacted_length: int
    redaction_count: int
    text: str


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #


def redact(text: str) -> str:
    """Apply all redaction patterns to *text* and return the scrubbed result."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_with_stats(text: str) -> RedactionResult:
    """Redact *text* and return detailed statistics alongside the cleaned result.

    Parameters
    ----------
    text : str
        Raw text potentially containing PII or secrets.

    Returns
    -------
    RedactionResult
        ``text`` — redacted string.
        ``redaction_count`` — total number of substitutions made.
    """
    original = text
    count = 0
    for pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return RedactionResult(
        original_length=len(original),
        redacted_length=len(text),
        redaction_count=count,
        text=text,
    )
