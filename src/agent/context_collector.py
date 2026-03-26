"""Context collector — gathers full issue context from Jira and integrations."""

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
        jenkins_client=None,
        confluence_client=None,
        pipeline_correlator=None,
    ):
        self.jira_client = jira_client or JiraClient()
        self._rag_engine = rag_engine
        self._log_lookup = log_lookup
        self._testrail = testrail_client
        self._git_client = git_client
        self._s3_fetcher = s3_fetcher
        self._jenkins = jenkins_client
        self._confluence = confluence_client
        self._correlator = pipeline_correlator

    # Public API
    def collect(
        self,
        issue_key: str,
        max_comments: int = DEFAULT_COMMENT_COUNT,
    ) -> ContextCollectionResult:
        """Collect all available context for an issue."""
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

            # 14. TestRail results by marker (issue key as marker)
            testrail_marker_results = self._fetch_testrail_by_marker(
                issue_key, issue_data
            )
            # 15. Confluence citations (text search for issue key + components)
            confluence_citations = self._fetch_confluence_citations(
                issue_key, issue_data
            )
            # 16. Jenkins test reports (parsed JUnit XML)
            jenkins_test_report = self._fetch_jenkins_test_report(jenkins_links)
            # 17. Jenkins console errors (structured extraction)
            jenkins_console_errors = self._fetch_jenkins_console_errors(
                jenkins_links
            )
            # 18. Jenkins build info (full metadata)
            jenkins_build_info = self._fetch_jenkins_build_info(jenkins_links)
            # 19. Build pipeline correlation
            pipeline_correlation = self._run_pipeline_correlation(
                issue_key, issue_data, jenkins_links, git_prs
            )

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
                testrail_marker_results=testrail_marker_results or None,
                confluence_citations=confluence_citations or None,
                jenkins_test_report=jenkins_test_report,
                jenkins_console_errors=jenkins_console_errors,
                jenkins_build_info=jenkins_build_info or None,
                pipeline_correlation=(
                    pipeline_correlation.to_dict()
                    if pipeline_correlation and pipeline_correlation.has_data
                    else None
                ),
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
        """Detect PR references and fetch metadata across configured repos."""
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
            # Use multi-repo fan-out (falls back to single-repo internally)
            prs, repos_searched = self._git_client.fetch_prs_across_repos(
                issue_text=desc,
                comment_texts=comment_bodies,
                max_prs_per_repo=3,
            )
            # Stash searched repos for later use by the draft
            self._last_repos_searched = repos_searched
            return prs
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

    # Ground-truth helpers

    def _fetch_testrail_by_marker(
        self,
        issue_key: str,
        issue_data: dict[str, Any],
    ) -> Optional[list[dict[str, Any]]]:
        """Fetch TestRail results filtered by marker (issue key in refs)."""
        if not self._testrail or not getattr(self._testrail, "enabled", False):
            return None

        try:
            runs = self._testrail.get_runs(limit=5)
            results: list[dict[str, Any]] = []
            for run in runs[:3]:
                run_id = run.get("id")
                if not run_id:
                    continue
                # Try marker-scoped summary first
                summary = self._testrail.get_run_summary_by_marker(
                    run_id, issue_key
                )
                if summary and summary.get("total", 0) > 0:
                    results.append(summary)

            # Fallback: if no marker matches, include overall run summaries
            if not results:
                for run in runs[:2]:
                    run_id = run.get("id")
                    if not run_id:
                        continue
                    try:
                        full_summary = self._testrail.get_run_summary(run_id)
                        if full_summary and full_summary.get("total", 0) > 0:
                            results.append(full_summary)
                    except Exception as exc:
                        logger.debug(
                            "TestRail run %d summary fallback failed: %s",
                            run_id, exc,
                        )

            return results or None
        except Exception as exc:
            logger.warning("TestRail by-marker fetch failed: %s", exc)
            return None

    def _fetch_confluence_citations(
        self,
        issue_key: str,
        issue_data: dict[str, Any],
    ) -> Optional[list[dict[str, str]]]:
        """Search Confluence for pages related to the issue."""
        if not self._confluence or not getattr(self._confluence, "enabled", False):
            return None

        fields = issue_data.get("fields", {})
        summary = fields.get("summary", "")
        components = [
            c.get("name", "") for c in fields.get("components", [])
        ]

        citations: list[dict[str, str]] = []
        queries = [issue_key]
        if components:
            queries.append(components[0])
        elif summary:
            # Use first 3 significant words from summary
            words = [w for w in summary.split() if len(w) > 3][:3]
            if words:
                queries.append(" ".join(words))

        for query in queries[:2]:
            try:
                results = self._confluence.search_by_text(query, limit=3)
                for cite in results:
                    cite_dict = cite.to_citation_dict()
                    # Deduplicate by page_id
                    if not any(
                        c.get("url") == cite_dict.get("url")
                        for c in citations
                    ):
                        citations.append(cite_dict)
            except Exception as exc:
                logger.debug("Confluence search for %r failed: %s", query, exc)

        return citations or None

    def _fetch_jenkins_test_report(
        self, jenkins_links: Optional[list[str]]
    ) -> Optional[dict[str, Any]]:
        """Parse JUnit test reports from Jenkins builds."""
        if not self._jenkins or not jenkins_links:
            return None
        if not getattr(self._jenkins, "enabled", False):
            return None

        for url in jenkins_links[:2]:
            try:
                report = self._jenkins.parse_test_report(url)
                if report:
                    return report.to_dict()
            except Exception as exc:
                logger.debug("Jenkins test report parse failed for %s: %s", url, exc)
        return None

    def _fetch_jenkins_console_errors(
        self, jenkins_links: Optional[list[str]]
    ) -> Optional[dict[str, Any]]:
        """Extract structured errors from Jenkins console output."""
        if not self._jenkins or not jenkins_links:
            return None
        if not getattr(self._jenkins, "enabled", False):
            return None

        for url in jenkins_links[:2]:
            try:
                errors = self._jenkins.parse_console_errors(url)
                if errors and (errors.error_lines or errors.exception_blocks):
                    return errors.to_dict()
            except Exception as exc:
                logger.debug("Jenkins console error parse failed for %s: %s", url, exc)
        return None

    def _fetch_jenkins_build_info(
        self, jenkins_links: Optional[list[str]]
    ) -> Optional[list[dict[str, Any]]]:
        """Fetch full build metadata from Jenkins."""
        if not self._jenkins or not jenkins_links:
            return None
        if not getattr(self._jenkins, "enabled", False):
            return None

        builds: list[dict[str, Any]] = []
        for url in jenkins_links[:3]:
            try:
                info = self._jenkins.get_build_info(url)
                if info:
                    builds.append(info.to_dict())
            except Exception as exc:
                logger.debug("Jenkins build info failed for %s: %s", url, exc)
        return builds or None

    def _run_pipeline_correlation(
        self,
        issue_key: str,
        issue_data: dict[str, Any],
        jenkins_links: Optional[list[str]],
        git_prs: Optional[list],
    ):
        """Run the full pipeline correlation (Jira → Git → Jenkins → TestRail)."""
        if not self._correlator or not getattr(self._correlator, "enabled", False):
            return None

        try:
            return self._correlator.correlate(
                issue_key=issue_key,
                issue_data=issue_data,
                jenkins_links=jenkins_links,
                git_prs=git_prs,
            )
        except Exception as exc:
            logger.warning("Pipeline correlation failed: %s", exc)
            return None

    def _fetch_testrail_results(
        self, issue_data: dict[str, Any]
    ) -> Optional[list[dict[str, Any]]]:
        if not self._testrail:
            return None
        if not getattr(self._testrail, "enabled", False):
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

        results: list[dict[str, Any]] = []

        if run_ids:
            # Fetch summaries for explicitly referenced runs
            for rid in list(run_ids)[:3]:
                try:
                    summary = self._testrail.get_run_summary(rid)
                    results.append(summary)
                except Exception as exc:
                    logger.warning("TestRail run %d fetch failed: %s", rid, exc)
        else:
            # Fallback: fetch the most recent run(s) for the configured project
            try:
                recent_runs = self._testrail.get_runs(limit=3)
                for run in recent_runs[:2]:
                    rid = run.get("id")
                    if rid:
                        try:
                            summary = self._testrail.get_run_summary(rid)
                            results.append(summary)
                        except Exception as exc:
                            logger.warning("TestRail run %d fetch failed: %s", rid, exc)
            except Exception as exc:
                logger.warning("TestRail recent-runs fallback failed: %s", exc)

        return results or None

    @staticmethod
    def _infer_author_role(comment: dict[str, Any]) -> str:
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
