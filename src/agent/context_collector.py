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

    def __init__(
        self,
        jira_client: Optional[JiraClient] = None,
        rag_engine=None,
        log_lookup=None,
        testrail_client=None,
        git_client=None,
        s3_fetcher=None,
    ):
        self.jira_client = jira_client or JiraClient()
        self._rag_engine = rag_engine
        self._log_lookup = log_lookup
        self._testrail = testrail_client
        self._git_client = git_client
        self._s3_fetcher = s3_fetcher

    # Public API
    def collect(
        self,
        issue_key: str,
        max_comments: int = DEFAULT_COMMENT_COUNT,
    ) -> ContextCollectionResult:
        """ Collect all available context for an issue. """
        start_time = datetime.now(timezone.utc)

        try:
            # 1. Full issue JSON
            issue_data = self.jira_client.get_issue(issue_key)
            fields = issue_data.get("fields", {})

            # 2. Last N comments (with author_role heuristic)
            last_comments_raw = self.jira_client.get_last_comments(
                issue_key, n=max_comments
            )
            last_comments = [
                CommentSnapshot(
                    comment_id=c.get("id", ""),
                    author=c.get("author", {}).get("displayName", "unknown"),
                    author_role=self._infer_author_role(c),
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
            # 7. RAG snippets (search against indexed docs)
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
            # 13. S3 artifacts (build artifacts stored for the detected build ID)
            s3_artifacts = self._fetch_s3_artifacts(build_metadata)

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
                rag_snippets=rag_snippets or None,
                log_entries=log_entries or None,
                testrail_results=testrail_results or None,
                build_metadata=build_metadata,
                git_prs=git_prs or None,
                elk_log_entries=elk_log_entries or None,
                s3_artifacts=s3_artifacts or None,
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

    def _fetch_s3_artifacts(
        self, build_metadata: Optional[dict[str, str]]
    ) -> list[dict]:
        """Fetch S3 build artifacts for the build ID found in *build_metadata*."""
        if not self._s3_fetcher or not getattr(self._s3_fetcher, "enabled", False):
            return []

        build_id = None
        if build_metadata:
            build_id = build_metadata.get("version") or build_metadata.get("commit")

        if not build_id:
            return []

        try:
            artifacts = self._s3_fetcher.fetch_artifacts_for_build(build_id)
            return [a.to_dict() for a in artifacts]
        except Exception as exc:
            logger.warning("S3 artifact fetch failed for build %s: %s", build_id, exc)
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

    @staticmethod
    def _infer_author_role(comment: dict[str, Any]) -> str:
        """Infer author role from Jira comment metadata.

        Uses the author's display name and email domain as a heuristic:
          - Contains 'qa' or 'test'  → QA
          - Contains 'devops' or 'ops' or 'sre' → DevOps
          - Otherwise → Developer

        Falls back to 'Developer' when no signal is available.
        """
        author = comment.get("author", {})
        display_name = (author.get("displayName") or "").lower()
        email = (author.get("emailAddress") or "").lower()
        combined = f"{display_name} {email}"

        if any(k in combined for k in ("qa", "test", "quality", "tester")):
            return "QA"
        if any(k in combined for k in ("devops", "ops", "sre", "infra", "platform")):
            return "DevOps"
        return "Developer"

    def _query_rag(
        self,
        issue_key: str,
        summary: str,
        description: str,
    ) -> list:
        """Query the RAG engine for relevant snippets.

        Runs two queries:
          1. Confluence / PDF knowledge base (summary + description)
          2. Prior similar defects (summary only, tagged source=jira)

        Returns a combined de-duplicated list capped at RAG top_k * 2.
        Returns an empty list if RAG is not configured or fails.
        """
        if self._rag_engine is None:
            return []

        kb_query = f"{summary}. {description[:500]}" if description else summary
        snippets: list = []

        # 1. Knowledge-base query (Confluence, PDFs, runbooks)
        try:
            result = self._rag_engine.query(text=kb_query)
            snippets.extend(result.snippets)
        except Exception as exc:
            logger.warning("RAG KB query failed for %s: %s", issue_key, exc)
        # 2. Prior similar defects query
        try:
            prior_result = self._rag_engine.query(
                text=summary,
                where={"source": "jira"},
            )
            # Avoid duplicates by chunk id
            existing_ids = {getattr(s, "chunk_id", None) for s in snippets}
            for s in prior_result.snippets:
                if getattr(s, "chunk_id", None) not in existing_ids:
                    snippets.append(s)
        except Exception as exc:
            # prior-defect index may not exist yet — non-fatal
            logger.debug("RAG prior-defect query failed for %s: %s", issue_key, exc)

        return snippets

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
