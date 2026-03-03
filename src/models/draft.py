"""Draft response models"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict


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
    
    # Approval tracking
    status: DraftStatus = DraftStatus.GENERATED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    posted_at: Optional[datetime] = None

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
