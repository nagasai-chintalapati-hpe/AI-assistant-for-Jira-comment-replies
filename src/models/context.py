"""Context collection data models"""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel


class IssueContext(BaseModel):
    """Represents collected context for a Jira issue"""

    issue_key: str
    summary: str
    description: str
    issue_type: str
    status: str
    priority: str
    environment: Optional[str] = None
    versions: Optional[list[str]] = None
    components: Optional[list[str]] = None
    labels: Optional[list[str]] = None
    
    # Relations
    linked_issues: Optional[list[dict[str, str]]] = None  # {key, type, status}
    attached_files: Optional[list[dict[str, Any]]] = None  # {name, url, type}
    
    # History
    changelog: Optional[list[dict[str, Any]]] = None
    comment_count: int = 0
    
    # Related artifacts
    git_pr_links: Optional[list[dict[str, str]]] = None  # {pr_id, status, url}
    testrail_runs: Optional[list[dict[str, str]]] = None  # {run_id, result, url}


class ContextCollectionResult(BaseModel):
    """Result of context collection with source tracking"""

    issue_context: IssueContext
    rag_results: Optional[list[dict[str, str]]] = None  # {source, content, relevance}
    available_logs: Optional[list[str]] = None
    collection_timestamp: datetime
    collection_duration_ms: float

    class Config:
        json_schema_extra = {
            "example": {
                "issue_context": {
                    "issue_key": "DEFECT-123",
                    "summary": "UI crashes on Chrome when uploading large file",
                    "priority": "High",
                },
                "rag_results": [
                    {
                        "source": "Confluence: Upload Handler Design",
                        "content": "Max file size is 100MB...",
                        "relevance": 0.95,
                    }
                ],
                "collection_timestamp": "2025-02-23T10:35:00Z",
                "collection_duration_ms": 1250.5,
            }
        }
