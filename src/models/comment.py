"""Comment data model"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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
