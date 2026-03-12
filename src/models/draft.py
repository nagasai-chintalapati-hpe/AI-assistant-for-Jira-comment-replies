"""Draft response models"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class DraftStatus(str, Enum):
    """Draft status"""

    GENERATED = "generated"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"


class Draft(BaseModel):
    """Represents a draft response to a Jira comment"""

    draft_id: str
    issue_key: str
    in_reply_to_comment_id: str
    created_at: datetime
    created_by: str  # System user/email

    # Draft content
    body: str
    suggested_actions: Optional[list[dict[str, str]]] = None  # {action, value}
    suggested_labels: Optional[list[str]] = None
    confidence_score: float  # 0.0 to 1.0

    # Evidence tracking
    citations: Optional[list[dict[str, str]]] = None  # {source, url, excerpt}
    evidence_used: Optional[list[str]] = None  # list of source descriptions used
    missing_info: Optional[list[str]] = None  # what info is still needed

    # Classification tracking
    classification_type: Optional[str] = None
    classification_reasoning: Optional[str] = None

    # Approval tracking
    status: DraftStatus = DraftStatus.GENERATED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    posted_at: Optional[datetime] = None
