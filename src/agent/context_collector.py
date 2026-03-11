"""Context collector – gathers full issue context.

Collects:
  • Issue fields (summary, description, environment, versions, components, labels)
  • Last N comments (default 10)
  • Attachment metadata
  • Linked issues
  • Changelog (status transitions)
  • Jenkins console-log URLs (heuristic detection)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.models.context import (
    CommentSnapshot,
    IssueContext,
    ContextCollectionResult,
)
from src.integrations.jira import JiraClient
import logging

logger = logging.getLogger(__name__)

DEFAULT_COMMENT_COUNT = 10


class ContextCollector:
    """Collects context from Jira and related systems."""

    def __init__(self, jira_client: Optional[JiraClient] = None):
        self.jira_client = jira_client or JiraClient()

    # Public API

    def collect(
        self,
        issue_key: str,
        max_comments: int = DEFAULT_COMMENT_COUNT,
    ) -> ContextCollectionResult:
        """
        Collect all available context for an issue.

        Args:
            issue_key: The Jira issue key (e.g., "DEFECT-123")
            max_comments: How many recent comments to include (default 10)

        Returns:
            ContextCollectionResult with issue context and evidence pointers
        """
        start_time = datetime.now(timezone.utc)

        try:
            # 1. Full issue JSON
            issue_data = self.jira_client.get_issue(issue_key)
            fields = issue_data.get("fields", {})

            # 2. Last N comments
            last_comments_raw = self.jira_client.get_last_comments(
                issue_key, n=max_comments
            )
            last_comments = [
                CommentSnapshot(
                    comment_id=c.get("id", ""),
                    author=c.get("author", {}).get("displayName", "unknown"),
                    author_role=None,
                    created=c.get("created", ""),
                    body=c.get("body", ""),
                )
                for c in last_comments_raw
            ]

            # 3. Attachments
            attachments = self.jira_client.get_attachments(issue_key)

            # 4. Linked issues
            linked_issues = self.jira_client.get_linked_issues(issue_key)

            # 5. Changelog
            changelog = self.jira_client.get_changelog(issue_key)

            # 6. Jenkins links
            jenkins_links = self.jira_client.detect_jenkins_links(issue_key)

            # Build IssueContext
            issue_context = IssueContext(
                issue_key=issue_key,
                summary=fields.get("summary", ""),
                description=fields.get("description", "") or "",
                issue_type=fields.get("issuetype", {}).get("name", ""),
                status=fields.get("status", {}).get("name", ""),
                priority=fields.get("priority", {}).get("name", ""),
                environment=fields.get("environment") or "",
                versions=self._extract_versions(fields),
                components=self._extract_components(fields),
                labels=fields.get("labels", []),
                linked_issues=linked_issues,
                attached_files=attachments,
                last_comments=last_comments,
                changelog=changelog,
                comment_count=len(
                    fields.get("comment", {}).get("comments", [])
                ),
            )

            elapsed_ms = (
                datetime.now(timezone.utc) - start_time
            ).total_seconds() * 1000

            return ContextCollectionResult(
                issue_context=issue_context,
                jenkins_links=jenkins_links or None,
                collection_timestamp=datetime.now(timezone.utc),
                collection_duration_ms=elapsed_ms,
            )

        except Exception as e:
            logger.error("Error collecting context for %s: %s", issue_key, e)
            raise

    # Private helpers

    @staticmethod
    def _extract_versions(fields: dict) -> list[str]:
        """Extract version information (affected + fix versions, deduplicated)."""
        versions: list[str] = []
        if "versions" in fields:
            versions.extend(v.get("name", "") for v in fields["versions"])
        if "fixVersions" in fields:
            versions.extend(v.get("name", "") for v in fields["fixVersions"])
        return list(set(versions))

    @staticmethod
    def _extract_components(fields: dict) -> list[str]:
        """Extract component names."""
        return [c.get("name", "") for c in fields.get("components", [])]
