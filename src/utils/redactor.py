"""PII and secret redactor — scrubs sensitive patterns before LLM processing."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Authorization headers
    (
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"),
        r"\1[REDACTED]",
    ),
    # Key=value credential pairs
    (
        re.compile(
            r"(?i)"
            r"(password|passwd|pwd|secret|api[_\-]?key|auth[_\-]?token"
            r"|access[_\-]?token|private[_\-]?key|client[_\-]?secret)"
            r"\s*[=:]\s*\S+"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)(https?://)([^:/@\s]+):([^@\s]+)@"),
        r"\1[USER]:[REDACTED]@",
    ),
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[EMAIL]",
    ),
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


def redact(text: str) -> str:
    """Apply all redaction patterns to *text* and return the scrubbed result."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_with_stats(text: str) -> RedactionResult:
    """Redact *text* and return detailed statistics alongside the cleaned result."""
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
