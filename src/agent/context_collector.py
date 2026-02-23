"""Context collector - gathers information about an issue"""

from datetime import datetime
from src.models.context import IssueContext, ContextCollectionResult
from src.integrations.jira import JiraClient
import logging

logger = logging.getLogger(__name__)


class ContextCollector:
    """Collects context from Jira and related systems"""
    
    def __init__(self):
        self.jira_client = JiraClient()
    
    def collect(self, issue_key: str) -> ContextCollectionResult:
        """
        Collect all available context for an issue.
        
        Args:
            issue_key: The Jira issue key (e.g., "DEFECT-123")
        
        Returns:
            ContextCollectionResult with issue context and RAG results
        """
        start_time = datetime.utcnow()
        
        try:
            # Fetch issue from Jira
            issue_data = self.jira_client.get_issue(issue_key)
            
            # Extract fields
            fields = issue_data.get("fields", {})
            
            issue_context = IssueContext(
                issue_key=issue_key,
                summary=fields.get("summary", ""),
                description=fields.get("description", ""),
                issue_type=fields.get("issuetype", {}).get("name", ""),
                status=fields.get("status", {}).get("name", ""),
                priority=fields.get("priority", {}).get("name", ""),
                environment=fields.get("environment", ""),
                versions=self._extract_versions(fields),
                components=self._extract_components(fields),
                labels=fields.get("labels", []),
                linked_issues=self._extract_linked_issues(issue_data),
                attached_files=self._extract_attachments(fields),
                comment_count=len(fields.get("comment", {}).get("comments", [])),
            )
            
            # TODO: Fetch RAG results from knowledge base
            rag_results = []
            
            # TODO: Collect logs if available
            available_logs = []
            
            collection_time = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            return ContextCollectionResult(
                issue_context=issue_context,
                rag_results=rag_results,
                available_logs=available_logs,
                collection_timestamp=datetime.utcnow(),
                collection_duration_ms=collection_time,
            )
        
        except Exception as e:
            logger.error(f"Error collecting context for {issue_key}: {e}")
            raise
    
    def _extract_versions(self, fields: dict) -> list[str]:
        """Extract version information"""
        versions = []
        if "versions" in fields:
            versions.extend([v.get("name", "") for v in fields["versions"]])
        if "fixVersions" in fields:
            versions.extend([v.get("name", "") for v in fields["fixVersions"]])
        return list(set(versions))
    
    def _extract_components(self, fields: dict) -> list[str]:
        """Extract component information"""
        return [c.get("name", "") for c in fields.get("components", [])]
    
    def _extract_linked_issues(self, issue_data: dict) -> list[dict]:
        """Extract linked issues"""
        linked = []
        for link in issue_data.get("fields", {}).get("issuelinks", []):
            linked.append({
                "key": link.get("inwardIssue", link.get("outwardIssue", {})).get("key", ""),
                "type": link.get("type", {}).get("name", ""),
                "status": link.get("inwardIssue", link.get("outwardIssue", {})).get("fields", {}).get("status", {}).get("name", ""),
            })
        return linked
    
    def _extract_attachments(self, fields: dict) -> list[dict]:
        """Extract attachment information"""
        attachments = []
        for att in fields.get("attachment", []):
            attachments.append({
                "name": att.get("filename", ""),
                "url": att.get("content", ""),
                "type": att.get("mimeType", ""),
            })
        return attachments
