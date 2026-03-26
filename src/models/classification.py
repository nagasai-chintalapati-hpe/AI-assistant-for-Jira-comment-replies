"""Comment classification models"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict


class CommentType(str, Enum):
    """Classification buckets for developer comments on defect tickets."""

    CANNOT_REPRODUCE = "cannot_reproduce"   # Cannot Repro
    NEED_MORE_INFO = "need_more_info"       # Need Info / Logs
    FIXED_VALIDATE = "fixed_validate"       # Fixed — Validate
    BY_DESIGN = "by_design"                 # By Design
    DUPLICATE_FIXED = "duplicate_fixed"     # Duplicate / Already fixed in X
    BLOCKED_WAITING = "blocked_waiting"     # Blocked by dependency / Waiting for X
    CONFIG_ISSUE = "config_issue"           # Not a bug / Configuration issue
    OTHER = "other"                         # Fallback


class CommentClassification(BaseModel):
    """Classification result for a comment"""

    comment_id: str
    comment_type: CommentType
    confidence: float  # 0.0 to 1.0
    reasoning: str
    missing_context: Optional[list[str]] = None  # What info is missing
    suggested_questions: Optional[list[str]] = None  # Questions to ask for clarification

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "comment_id": "10000",
                "comment_type": "cannot_reproduce",
                "confidence": 0.92,
                "reasoning": "Developer states inability to reproduce with current environment",
                "missing_context": ["Environment details", "Browser version", "Reproduction steps"],
                "suggested_questions": [
                    "What is your environment setup (OS, version)?",
                    "Can you provide step-by-step reproduction steps?",
                ],
            }
        }
    )
