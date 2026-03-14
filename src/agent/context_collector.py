"""Context collector – gathers full issue context.

Collects:
  • Issue fields (summary, description, environment, versions, components, labels)
  • Last N comments (default 10)
  • Attachment metadata
  • Linked issues
  • Changelog (status transitions)
  • Jenkins console-log URLs (heuristic detection)
  • RAG snippets (semantic search against indexed documents)
  • Log entries (Jenkins console output / local log files)
  • ELK / OpenSearch log entries
  • TestRail results (failed / retest tests for related runs)
  • Git PR metadata (linked PRs from GitHub / GitLab / Bitbucket)
  • Build metadata (commit, version, deploy timestamp)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

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
        log_lookup=None,
        testrail_client=None,
        git_client=None,
    ):
        self.jira_client = jira_client or JiraClient()
        self._rag_engine = rag_engine
        self._log_lookup = log_lookup
        self._testrail = testrail_client
        self._git_client = git_client

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

            # 8. Log entries (Jenkins console output for detected URLs)
            log_entries = self._fetch_log_entries(jenkins_links)

            # 9. Build metadata (from first Jenkins URL)
            build_metadata = self._fetch_build_metadata(jenkins_links)

            # 10. TestRail results (if run IDs detected in comments/description)
            testrail_results = self._fetch_testrail_results(issue_data)

            # 11. Git PR metadata (PR refs detected in issue text + comments)
            git_prs = self._fetch_git_prs(issue_data)

            # 12. ELK log entries (search by summary keywords + build/env)
            elk_log_entries = self._fetch_elk_logs(
                issue_data=issue_data,
                build_metadata=build_metadata,
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
                log_entries=log_entries or None,
                testrail_results=testrail_results or None,
                build_metadata=build_metadata,
                git_prs=git_prs or None,
                elk_log_entries=elk_log_entries or None,
                collection_timestamp=datetime.now(timezone.utc),
                collection_duration_ms=elapsed_ms,
            )

        except Exception as e:
            logger.error("Error collecting context for %s: %s", issue_key, e)
            raise

    # Private helpers

    def _fetch_git_prs(
        self, issue_data: dict[str, Any]
    ) -> list:
        """Detect PR references in the issue and fetch Git PR metadata.

        Scans the issue description and all comments for PR number patterns.
        Returns a list of GitPRMetadata objects (up to 3).
        """
        if not self._git_client:
            return []

        fields = issue_data.get("fields", {})
        desc = fields.get("description", "") or ""
        comment_bodies: list[str] = [
            c.get("body", "")
            for c in fields.get("comment", {}).get("comments", [])
            if c.get("body")
        ]

        try:
            return self._git_client.fetch_prs_for_issue(
                issue_text=desc,
                comment_texts=comment_bodies,
                max_prs=3,
            )
        except Exception as exc:
            logger.warning("Git PR fetch failed: %s", exc)
            return []

    def _fetch_elk_logs(
        self,
        issue_data: dict[str, Any],
        build_metadata: Optional[dict[str, str]],
    ) -> list:
        """Search ELK for log entries related to the issue.

        Uses the issue summary as the search query, enriched with
        build_id and environment from build_metadata when available.
        """
        if not self._log_lookup or not getattr(self._log_lookup, "elk_enabled", False):
            return []

        fields = issue_data.get("fields", {})
        summary = fields.get("summary", "") or ""
        if not summary:
            return []

        # Try to extract useful keywords from summary (first 200 chars)
        query = summary[:200]

        build_id: Optional[str] = None
        env: Optional[str] = None
        if build_metadata:
            build_id = build_metadata.get("version") or build_metadata.get("commit")
            env = None  # environment comes from IssueContext, not build metadata

        # Use environment from issue fields if available
        issue_env = fields.get("environment") or ""
        if issue_env and isinstance(issue_env, str):
            env = issue_env[:100]

        try:
            return self._log_lookup.search_elk_logs(
                query=query,
                build_id=build_id,
                env=env or None,
            )
        except Exception as exc:
            logger.warning("ELK log search failed: %s", exc)
            return []

    def _fetch_log_entries(self, jenkins_links: list[str]) -> list:
        """Fetch Jenkins console output for detected URLs."""
        if not self._log_lookup or not jenkins_links:
            return []
        try:
            return self._log_lookup.fetch_jenkins_logs_for_urls(jenkins_links)
        except Exception as exc:
            logger.warning("Log fetch failed: %s", exc)
            return []

    def _fetch_build_metadata(
        self, jenkins_links: list[str]
    ) -> Optional[dict[str, str]]:
        """Extract build metadata from the first Jenkins URL."""
        if not self._log_lookup or not jenkins_links:
            return None
        try:
            return self._log_lookup.get_build_metadata(jenkins_links[0])
        except Exception as exc:
            logger.warning("Build metadata fetch failed: %s", exc)
            return None

    def _fetch_testrail_results(
        self, issue_data: dict[str, Any]
    ) -> Optional[list[dict[str, Any]]]:
        """Look for TestRail run IDs in the issue and fetch summaries.

        Searches the description and comments for patterns like
        ``R12345`` or ``run/12345`` to find TestRail run references.
        """
        if not self._testrail:
            return None

        run_ids: set[int] = set()
        fields = issue_data.get("fields", {})

        # Search description
        desc = fields.get("description", "") or ""
        if isinstance(desc, str):
            for m in re.findall(r"(?:R|run[/\s]?)(\d{4,})", desc, re.IGNORECASE):
                run_ids.add(int(m))

        # Search comments
        for c in fields.get("comment", {}).get("comments", []):
            body = c.get("body", "")
            if isinstance(body, str):
                for m in re.findall(r"(?:R|run[/\s]?)(\d{4,})", body, re.IGNORECASE):
                    run_ids.add(int(m))

        if not run_ids:
            return None

        results: list[dict[str, Any]] = []
        for rid in list(run_ids)[:3]:  # limit to 3 runs
            try:
                summary = self._testrail.get_run_summary(rid)
                results.append(summary)
            except Exception as exc:
                logger.warning("TestRail run %d fetch failed: %s", rid, exc)

        return results or None

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
