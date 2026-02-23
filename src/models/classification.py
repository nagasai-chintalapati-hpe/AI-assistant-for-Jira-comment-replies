"""Comment classification models"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class CommentType(str, Enum):
    """Enum for comment classification types"""

    CANNOT_REPRODUCE = "cannot_reproduce"
    NEED_MORE_INFO = "need_more_info"  # Includes logs, env details, steps
    AS_DESIGNED = "as_designed"
    DUPLICATE = "duplicate"
    NOT_A_BUG = "not_a_bug"
    FIX_READY = "fix_ready"
    BLOCKED = "blocked"
    STATUS_UPDATE = "status_update"
    OTHER = "other"


class CommentClassification(BaseModel):
    """Classification result for a comment"""

    comment_id: str
    comment_type: CommentType
    confidence: float  # 0.0 to 1.0
    reasoning: str
    missing_context: Optional[list[str]] = None  # What info is missing
    suggested_questions: Optional[list[str]] = None  # Questions to ask for clarification

    class Config:
        json_schema_extra = {
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
