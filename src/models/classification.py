"""Comment classification models"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class CommentType(str, Enum):
    """MVP v1 classification buckets."""

    CANNOT_REPRODUCE = "cannot_reproduce"  # Cannot Repro
    NEED_MORE_INFO = "need_more_info"  # Need Info / Logs
    FIXED_VALIDATE = "fixed_validate"  # Fixed — Validate
    BY_DESIGN = "by_design"  # By Design
    OTHER = "other"  # Fallback


class CommentClassification(BaseModel):
    """Classification result for a comment"""

    comment_id: str
    comment_type: CommentType
    confidence: float  # 0.0 to 1.0
    reasoning: str
    missing_context: Optional[list[str]] = None  # What info is missing
    suggested_questions: Optional[list[str]] = None  # Questions to ask for clarification
