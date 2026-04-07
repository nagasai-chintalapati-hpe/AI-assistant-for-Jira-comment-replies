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
from src.config import settings
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
            git_prs, repos_searched = self._fetch_git_prs(issue_data)
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
                repos_searched=repos_searched or None,
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
    ) -> tuple[list, list[str]]:
        """Detect PR references and fetch metadata across configured repos.

        Returns ``(prs, repos_searched)`` so both can be stored on the
        :class:`ContextCollectionResult`.
        """
        if not self._git_client:
            return [], []

        fields = issue_data.get("fields", {})
        issue_key = issue_data.get("key", "") or fields.get("project", {}).get("key", "")
        desc = fields.get("description", "") or ""
        comment_bodies: list[str] = [
            c.get("body", "")
            for c in fields.get("comment", {}).get("comments", [])
            if c.get("body")
        ]

        try:
            # Use multi-repo fan-out with issue-key fallback
            prs, repos_searched = self._git_client.fetch_prs_across_repos(
                issue_text=desc,
                comment_texts=comment_bodies,
                max_prs_per_repo=3,
                issue_key=issue_key or None,
            )
            return prs, repos_searched
        except Exception as exc:
            logger.warning("Git PR fetch failed: %s", exc)
            return [], []

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
        """Fetch TestRail results filtered by marker (issue key in refs),
        falling back to relevance-scored recent runs."""
        if not self._testrail or not getattr(self._testrail, "enabled", False):
            return None

        try:
            runs = self._testrail.get_runs(limit=10)
            results: list[dict[str, Any]] = []

            # First pass: try marker-scoped (issue key in test refs)
            for run in runs[:5]:
                run_id = run.get("id")
                if not run_id:
                    continue
                summary = self._testrail.get_run_summary_by_marker(
                    run_id, issue_key
                )
                if summary and summary.get("total", 0) > 0:
                    results.append(summary)

            # Fallback: rank all runs by keyword relevance to the defect
            if not results:
                keywords = self._extract_defect_keywords(issue_data)
                scored_runs = [
                    (run, self._score_run_relevance(run, keywords))
                    for run in runs
                ]
                scored_runs.sort(key=lambda x: x[1], reverse=True)

                for run, score in scored_runs[:3]:
                    run_id = run.get("id")
                    if not run_id or score <= 0:
                        continue
                    try:
                        full_summary = self._testrail.get_run_summary(run_id)
                        if full_summary and full_summary.get("total", 0) > 0:
                            full_summary["_relevance_score"] = score
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
        """Search Confluence for pages most relevant to the defect.

        Runs multiple targeted queries — issue key, product names,
        individual significant keywords, and technology acronyms — then
        deduplicates and ranks results so the most useful pages surface
        first.
        """
        if not self._confluence or not getattr(self._confluence, "enabled", False):
            return None

        fields = issue_data.get("fields", {}) or {}
        summary = fields.get("summary", "") or ""
        description = (fields.get("description", "") or "")[:2000]
        combined_text = f"{summary} {description}"
        components = [c.get("name", "") for c in fields.get("components", []) if c.get("name")]
        labels = fields.get("labels", []) or []
        versions = [
            v.get("name", "") for v in
            (fields.get("versions", []) or []) + (fields.get("fixVersions", []) or [])
            if v.get("name")
        ]

        # Reuse the shared keyword extractor
        defect_keywords = self._extract_defect_keywords(issue_data)

        # ── Build a prioritised list of search queries ──────────────────
        queries: list[tuple[str, int]] = []  # (query_text, priority_boost)

        # 1. Exact issue key (highest signal)
        queries.append((issue_key, 3))

        # 2. Component + version combos (e.g. "Morpheus 8.1.0")
        for comp in components[:2]:
            if versions:
                queries.append((f"{comp} {versions[0]}", 2))
            queries.append((comp, 1))

        # 3. Product names from description (e.g. "Morpheus", "HVM")
        product_matches = re.findall(
            r"(\b[A-Z][a-zA-Z]+)\s+(?:version|ver|v)[:\s]*(\d+\.\d+[\.\d]*)",
            combined_text, re.IGNORECASE,
        )
        for prod_name, prod_ver in product_matches:
            queries.append((f"{prod_name} {prod_ver}", 2))
            queries.append((prod_name, 1))

        # 4. Technology acronyms (HVM, VTEP, SDN, etc.)
        for acr in re.findall(r"\b([A-Z]{3,})\b", combined_text):
            if acr not in {"AND", "THE", "FOR", "NOT", "BUT", "HAS", "ARE",
                           "WAS", "URL", "HTTP", "HTTPS", "API", "SSH"}:
                queries.append((acr, 2))

        # 5. INDIVIDUAL significant words from summary (much better recall
        #    than 4-word phrases that rarely match)
        _STOP = {"the", "and", "for", "with", "from", "that", "this", "not",
                 "are", "was", "but", "has", "have", "had", "been", "will",
                 "does", "did", "can", "should", "would", "could", "issue",
                 "bug", "defect", "error", "when", "after", "before",
                 "into", "even", "one", "all", "some", "observed",
                 "getting", "showing", "successful"}
        sig_words = [w for w in re.split(r"[\s/\-_()\[\],.;:]+", summary)
                     if len(w) > 2 and w.lower() not in _STOP]

        # Each significant word as its own query (great for Confluence CQL)
        for w in sig_words[:6]:
            queries.append((w, 1))

        # 2-word combos from adjacent summary words (better precision)
        for i in range(len(sig_words) - 1):
            queries.append((f"{sig_words[i]} {sig_words[i+1]}", 1))
            if len(queries) > 15:
                break

        # 6. Error signatures in description/summary
        error_patterns = re.findall(
            r"(?:[A-Z][a-z]+(?:Exception|Error|Failure))"
            r"|(?:[a-z_]+\.[A-Z][a-zA-Z]+(?:Exception|Error))"
            r"|(?:status\s*(?:code)?\s*\d{3})",
            combined_text,
            re.IGNORECASE,
        )
        for ep in dict.fromkeys(error_patterns):
            queries.append((ep, 2))

        # 7. Labels that look like product/feature names
        for lbl in labels[:3]:
            if len(lbl) > 2 and lbl.lower() not in _STOP:
                queries.append((lbl, 1))

        # Deduplicate queries (case-insensitive)
        seen_queries: set[str] = set()
        unique_queries: list[tuple[str, int]] = []
        for q, boost in queries:
            ql = q.lower().strip()
            if ql and ql not in seen_queries:
                seen_queries.add(ql)
                unique_queries.append((q, boost))

        # ── Execute searches and collect scored results ─────────────────
        scored: dict[str, tuple[dict[str, str], int]] = {}

        for query_text, boost in unique_queries[:10]:  # cap to avoid rate-limiting
            try:
                results = self._confluence.search_by_text(query_text, limit=3)
                for cite in results:
                    cite_dict = cite.to_citation_dict()
                    url = cite_dict.get("url", "")
                    # Compute a relevance score
                    score = boost
                    title_lower = cite_dict.get("source", "").lower()
                    excerpt_lower = cite_dict.get("excerpt", "").lower()
                    page_text = f"{title_lower} {excerpt_lower}"
                    # Boost pages matching defect keywords
                    for kw in defect_keywords[:10]:
                        if kw.lower() in page_text:
                            score += 1
                    for comp in components:
                        if comp.lower() in page_text:
                            score += 2
                    for ver in versions[:2]:
                        if ver in page_text:
                            score += 1
                    if issue_key.lower() in page_text:
                        score += 3
                    # Keep highest score if already seen
                    if url not in scored or scored[url][1] < score:
                        scored[url] = (cite_dict, score)
            except Exception as exc:
                logger.debug("Confluence search for %r failed: %s", query_text, exc)

        if not scored:
            return None

        # Sort by score descending, return top 5
        ranked = sorted(scored.values(), key=lambda x: x[1], reverse=True)
        citations = [cite_dict for cite_dict, _score in ranked[:5]]

        logger.info(
            "Confluence: %d relevant pages found (from %d queries)",
            len(citations), min(len(unique_queries), 10),
        )
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

    @staticmethod
    def _extract_defect_keywords(issue_data: dict[str, Any]) -> list[str]:
        """Extract product/version/technology keywords from a defect for
        relevance matching against TestRail run names or other sources."""
        fields = issue_data.get("fields", {}) or {}
        summary = fields.get("summary", "") or ""
        description = (fields.get("description", "") or "")[:2000]
        combined = f"{summary} {description}"

        keywords: list[str] = []

        # Components and versions (highest signal)
        for c in fields.get("components", []):
            if c.get("name"):
                keywords.append(c["name"])
        for v in fields.get("versions", []) + fields.get("fixVersions", []):
            if v.get("name"):
                keywords.append(v["name"])

        # Product names from description (e.g. "Morpheus version: 8.0.11")
        for m in re.findall(
            r"(\b[A-Z][a-zA-Z]+)\s+(?:version|ver|v)[:\s]*(\d+\.\d+[\.\d]*)",
            combined, re.IGNORECASE,
        ):
            keywords.extend(m)  # product name + version number

        # Technology acronyms (3+ uppercase letters, e.g. HVM, VTEP, SDN)
        for acr in re.findall(r"\b([A-Z]{3,})\b", combined):
            if acr not in {"AND", "THE", "FOR", "NOT", "BUT", "HAS", "ARE",
                           "WAS", "URL", "HTTP", "HTTPS", "API"}:
                keywords.append(acr)

        # Version patterns (e.g. 8.1.0, Build 1157)
        for ver in re.findall(r"\b(\d+\.\d+(?:\.\d+)?)\b", combined):
            keywords.append(ver)
        for build in re.findall(r"Build\s*(\d+)", combined, re.IGNORECASE):
            keywords.append(f"Build {build}")
            keywords.append(build)

        # Significant words from summary (nouns/proper nouns)
        _STOP = {"the", "and", "for", "with", "from", "that", "this", "not",
                 "are", "was", "but", "has", "into", "after", "before", "when",
                 "while", "even", "one", "all", "some", "data", "getting",
                 "showing", "observed", "successful"}
        for w in re.split(r"[\s/\-_()\[\],.;:]+", summary):
            if len(w) > 2 and w.lower() not in _STOP:
                keywords.append(w)

        # Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for k in keywords:
            kl = k.lower().strip()
            if kl and kl not in seen:
                seen.add(kl)
                unique.append(k.strip())
        return unique

    @staticmethod
    def _score_run_relevance(
        run: dict[str, Any], keywords: list[str]
    ) -> int:
        """Score a TestRail run's relevance to a defect based on keyword overlap."""
        run_name = (run.get("name", "") or "").lower()
        run_desc = (run.get("description", "") or "").lower()
        run_text = f"{run_name} {run_desc}"
        score = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in run_text:
                # Version numbers and acronyms are stronger signals
                if re.match(r"\d+\.\d+", kw) or kw.isupper():
                    score += 3
                else:
                    score += 1
        return score

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
            # Fallback: fetch recent runs and rank by relevance to the defect
            try:
                recent_runs = self._testrail.get_runs(limit=10)
                keywords = self._extract_defect_keywords(issue_data)
                scored_runs = [
                    (run, self._score_run_relevance(run, keywords))
                    for run in recent_runs
                ]
                # Sort by relevance score descending
                scored_runs.sort(key=lambda x: x[1], reverse=True)

                for run, score in scored_runs[:3]:
                    rid = run.get("id")
                    if not rid:
                        continue
                    # Only include runs with at least some keyword overlap
                    if score > 0:
                        try:
                            summary = self._testrail.get_run_summary(rid)
                            summary["_relevance_score"] = score
                            results.append(summary)
                        except Exception as exc:
                            logger.warning("TestRail run %d fetch failed: %s", rid, exc)

                # If no relevant runs found, include the most recent as fallback
                if not results and recent_runs:
                    rid = recent_runs[0].get("id")
                    if rid:
                        try:
                            summary = self._testrail.get_run_summary(rid)
                            results.append(summary)
                        except Exception as exc:
                            logger.warning("TestRail run %d fetch failed: %s", rid, exc)

                logger.info(
                    "TestRail: %d relevant runs from %d total (keywords: %s)",
                    len(results), len(recent_runs), keywords[:5],
                )
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
        min_relevance = 0.40

        # 1. Knowledge-base query (Confluence, PDFs, runbooks)
        try:
            result = self._rag_engine.query(text=kb_query)
            snippets.extend(s for s in result.snippets if s.relevance_score >= min_relevance)
        except Exception as exc:
            logger.warning("RAG KB query failed for %s: %s", issue_key, exc)
        # 2. Prior similar defects query
        try:
            prior_result = self._rag_engine.query(
                text=summary,
                where={"source": "jira"},
            )
            existing_ids = {getattr(s, "chunk_id", None) for s in snippets}
            for s in prior_result.snippets:
                if s.relevance_score >= min_relevance and getattr(s, "chunk_id", None) not in existing_ids:
                    snippets.append(s)
        except Exception as exc:
            logger.debug("RAG prior-defect query failed for %s: %s", issue_key, exc)

        # Sort by relevance descending, keep top-k
        snippets.sort(key=lambda s: s.relevance_score, reverse=True)
        return snippets[:settings.rag.top_k]

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
