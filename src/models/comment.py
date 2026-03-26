"""Comment data model"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class Comment(BaseModel):
    """Represents a Jira comment"""

    comment_id: str
    issue_key: str
    author: str
    author_role: Optional[str] = None  # Developer, QA, DevOps, etc.
    created: datetime
    updated: datetime
    body: str
    is_internal: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "comment_id": "10000",
                "issue_key": "DEFECT-123",
                "author": "dev.user@company.com",
                "author_role": "Developer",
                "created": "2025-02-23T10:30:00Z",
                "updated": "2025-02-23T10:30:00Z",
                "body": "Cannot reproduce this on my machine. Need environment details.",
                "is_internal": False,
            }
        }
    )
