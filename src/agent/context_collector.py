"""Context collector – gathers full issue context.

Collects:
  • Issue fields (summary, description, environment, versions, components, labels)
  • Last N comments (default 10)
  • Attachment metadata
  • Linked issues
  • Changelog (status transitions)
  • Jenkins console-log URLs (heuristic detection)
  • RAG snippets (semantic search against indexed documents)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.integrations.jira import JiraClient, _ensure_text
from src.models.context import (
    CommentSnapshot,
    ContextCollectionResult,
    IssueContext,
)

logger = logging.getLogger(__name__)

DEFAULT_COMMENT_COUNT = 10


class ContextCollector:
    """Collects context from Jira and related systems."""

    def __init__(
        self,
        jira_client: Optional[JiraClient] = None,
        rag_engine=None,
    ):
        self.jira_client = jira_client or JiraClient()
        self._rag_engine = rag_engine

    # Public API

    def collect(
        self,
        issue_key: str,
        max_comments: int = DEFAULT_COMMENT_COUNT,
    ) -> ContextCollectionResult:
        """
        Collect all available context for an issue.

        Args:
            issue_key: The Jira issue key (e.g., "PROJ-123")
            max_comments: How many recent comments to include (default 10)

        Returns:
            ContextCollectionResult with issue context and evidence pointers
        """
        start_time = datetime.now(timezone.utc)

        try:
            # 1. Single API call – fetch full issue JSON
            issue_data = self.jira_client.get_issue(issue_key)
            fields = issue_data.get("fields", {})

            # 2. Last N comments (extracted from the already-fetched data)
            all_comments = fields.get("comment", {}).get("comments", [])
            last_comments_raw = all_comments[-max_comments:]
            last_comments = [
                CommentSnapshot(
                    comment_id=c.get("id", ""),
                    author=c.get("author", {}).get("displayName", "unknown"),
                    author_role=None,
                    created=c.get("created", ""),
                    body=_ensure_text(c.get("body", "")),
                )
                for c in last_comments_raw
            ]

            attachments = self.jira_client.extract_attachments(issue_data)
            linked_issues = self.jira_client.extract_linked_issues(issue_data)
            changelog = self.jira_client.extract_changelog(issue_data)
            jenkins_links = self.jira_client.extract_jenkins_links(issue_data)

            # Fetch Jenkins console logs (best-effort, non-blocking)
            jenkins_log_snippets: dict[str, str] = {}
            if jenkins_links:
                jenkins_log_snippets = self.jira_client.fetch_jenkins_logs(
                    jenkins_links,
                    max_chars=3000,
                    timeout=10,
                )

            # 7. RAG snippets (semantic search against indexed docs)
            rag_snippets = self._query_rag(
                issue_key=issue_key,
                summary=fields.get("summary", ""),
                description=fields.get("description", "") or "",
            )

            # Build IssueContext
            issue_context = IssueContext(
                issue_key=issue_key,
                summary=fields.get("summary", ""),
                description=_ensure_text(fields.get("description", "")),
                issue_type=fields.get("issuetype", {}).get("name", ""),
                status=fields.get("status", {}).get("name", ""),
                priority=fields.get("priority", {}).get("name", ""),
                environment=_ensure_text(fields.get("environment", "")),
                versions=self._extract_versions(fields),
                components=self._extract_components(fields),
                labels=fields.get("labels", []),
                linked_issues=linked_issues,
                attached_files=attachments,
                last_comments=last_comments,
                changelog=changelog,
                comment_count=len(fields.get("comment", {}).get("comments", [])),
            )

            elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

            return ContextCollectionResult(
                issue_context=issue_context,
                jenkins_links=jenkins_links or None,
                rag_snippets=rag_snippets or None,
                collection_timestamp=datetime.now(timezone.utc),
                collection_duration_ms=elapsed_ms,
            )

        except Exception as e:
            logger.error("Error collecting context for %s: %s", issue_key, e)
            raise

    # Private helpers

    def _query_rag(
        self,
        issue_key: str,
        summary: str,
        description: str,
    ) -> list:
        """Query the RAG engine for relevant snippets.

        Builds a query from the issue summary + truncated description.
        Returns an empty list if RAG is not configured or fails.
        """
        if self._rag_engine is None:
            return []

        query_text = f"{summary}. {description[:500]}" if description else summary
        try:
            result = self._rag_engine.query(text=query_text)
            return result.snippets
        except Exception as exc:
            logger.warning("RAG query failed for %s: %s", issue_key, exc)
            return []

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
