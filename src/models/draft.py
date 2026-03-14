"""Draft response models"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict


class DraftStatus(str, Enum):
    """Draft status"""

    GENERATED = "generated"
    APPROVED = "approved"
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

    # Quality tracking
    rating: Optional[int] = None        # 1–5 stars; set by human reviewer
    hallucination_flag: bool = False     # True if draft has specific claims but no evidence

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "draft_id": "draft_1708688400",
                "issue_key": "DEFECT-123",
                "in_reply_to_comment_id": "10000",
                "created_at": "2025-02-23T10:35:00Z",
                "body": "Thanks for reporting. To help us reproduce this issue...",
                "suggested_labels": ["needs-info", "environment-setup"],
                "citations": [
                    {
                        "source": "Jenkins Build Log",
                        "excerpt": "Build #42 passed all tests",
                    }
                ],
                "status": "generated",
            }
        }
    )
