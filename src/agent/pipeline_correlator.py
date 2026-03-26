"""Build Pipeline Correlator — ties Jira → Git → Jenkins → TestRail."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


#  Data models 

@dataclass
class PipelineEvent:
    """A single event in the CI/CD pipeline timeline."""

    source: str        
    event_type: str      
    timestamp: Optional[str] = None
    summary: str = ""
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineCorrelation:
    """Unified view of an issue's CI/CD pipeline lifecycle."""

    issue_key: str
    commit_shas: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    pr_numbers: list[int] = field(default_factory=list)
    jenkins_builds: list[dict[str, Any]] = field(default_factory=list)
    testrail_runs: list[dict[str, Any]] = field(default_factory=list)
    confluence_citations: list[dict[str, str]] = field(default_factory=list)
    timeline: list[PipelineEvent] = field(default_factory=list)
    correlation_timestamp: str = ""

    @property
    def has_data(self) -> bool:
        return bool(
            self.commit_shas
            or self.jenkins_builds
            or self.testrail_runs
            or self.confluence_citations
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "commit_shas": self.commit_shas,
            "branches": self.branches,
            "pr_numbers": self.pr_numbers,
            "jenkins_build_count": len(self.jenkins_builds),
            "testrail_run_count": len(self.testrail_runs),
            "confluence_citation_count": len(self.confluence_citations),
            "timeline_event_count": len(self.timeline),
            "correlation_timestamp": self.correlation_timestamp,
        }

    def to_evidence_list(self) -> list[str]:
        """Convert to a flat list of evidence strings for the drafter."""
        evidence: list[str] = []
        for build in self.jenkins_builds:
            name = build.get("job_name", "build")
            result = build.get("result", "?")
            evidence.append(f"Jenkins: {name} — {result}")
        for run in self.testrail_runs:
            name = run.get("name", "run")
            rate = run.get("pass_rate", 0)
            evidence.append(f"TestRail: {name} — {rate}% pass")
        for cite in self.confluence_citations:
            evidence.append(f"Confluence: {cite.get('source', 'page')}")
        return evidence


#  Correlator service 

class BuildPipelineCorrelator:
    """Correlates a Jira issue across the CI/CD pipeline.  """

    def __init__(
        self,
        git_client=None,
        jenkins_client=None,
        testrail_client=None,
        confluence_client=None,
    ) -> None:
        self._git = git_client
        self._jenkins = jenkins_client
        self._testrail = testrail_client
        self._confluence = confluence_client

    @property
    def enabled(self) -> bool:
        return any([
            self._git and getattr(self._git, "enabled", False),
            self._jenkins and getattr(self._jenkins, "enabled", False),
            self._testrail and getattr(self._testrail, "enabled", False),
            self._confluence and getattr(self._confluence, "enabled", False),
        ])

    def correlate(
        self,
        issue_key: str,
        issue_data: dict[str, Any],
        jenkins_links: Optional[list[str]] = None,
        git_prs: Optional[list] = None,
    ) -> PipelineCorrelation:
        """Build a full pipeline correlation for *issue_key*."""
        result = PipelineCorrelation(
            issue_key=issue_key,
            correlation_timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # 1. Git layer — collect commits and branches
        self._correlate_git(result, issue_data, git_prs)

        # 2. Jenkins layer — build info and artifacts
        self._correlate_jenkins(result, jenkins_links)

        # 3. TestRail layer — find runs matching builds
        self._correlate_testrail(result, issue_key)

        # 4. Confluence layer — search for related docs
        self._correlate_confluence(result, issue_key, issue_data)

        # 5. Sort timeline by timestamp
        result.timeline.sort(
            key=lambda e: e.timestamp or "",
            reverse=True,
        )

        if result.has_data:
            logger.info(
                "Pipeline correlation for %s: %d commits, %d builds, "
                "%d runs, %d docs",
                issue_key,
                len(result.commit_shas),
                len(result.jenkins_builds),
                len(result.testrail_runs),
                len(result.confluence_citations),
            )

        return result

    #  Git correlation 

    def _correlate_git(
        self,
        result: PipelineCorrelation,
        issue_data: dict[str, Any],
        git_prs: Optional[list],
    ) -> None:
        """Extract commit SHAs and branch names from Git PRs."""
        if not git_prs:
            return

        for pr in git_prs:
            result.pr_numbers.append(pr.pr_number)
            if pr.merge_commit_sha:
                result.commit_shas.append(pr.merge_commit_sha)
            if pr.head_branch:
                result.branches.append(pr.head_branch)

            result.timeline.append(
                PipelineEvent(
                    source="git",
                    event_type=f"pr_{pr.state}",
                    timestamp=pr.merged_at or pr.created_at,
                    summary=f"PR #{pr.pr_number}: {pr.pr_title}",
                    url=pr.pr_url,
                    metadata={
                        "state": pr.state,
                        "commit": pr.merge_commit_sha or "",
                        "branch": pr.head_branch,
                    },
                )
            )

    #  Jenkins correlation 

    def _correlate_jenkins(
        self,
        result: PipelineCorrelation,
        jenkins_links: Optional[list[str]],
    ) -> None:
        """Fetch build info from Jenkins URLs and parse artifacts."""
        if not self._jenkins or not getattr(self._jenkins, "enabled", False):
            return
        if not jenkins_links:
            return

        for url in jenkins_links[:5]:
            try:
                build_info = self._jenkins.get_build_info(url)
                if not build_info:
                    continue

                build_dict = build_info.to_dict()
                result.jenkins_builds.append(build_dict)

                # Also collect commit SHA from Jenkins if not already known
                if build_info.commit_sha and build_info.commit_sha not in result.commit_shas:
                    result.commit_shas.append(build_info.commit_sha)
                if build_info.branch and build_info.branch not in result.branches:
                    result.branches.append(build_info.branch)

                result.timeline.append(
                    PipelineEvent(
                        source="jenkins",
                        event_type=f"build_{(build_info.result or 'unknown').lower()}",
                        timestamp=build_info.timestamp,
                        summary=(
                            f"{build_info.job_name} #{build_info.build_number} "
                            f"— {build_info.result}"
                        ),
                        url=build_info.url,
                        metadata=build_dict,
                    )
                )

                # Parse test reports if available
                test_report = self._jenkins.parse_test_report(url)
                if test_report:
                    build_dict["test_report"] = test_report.to_dict()
                    result.timeline.append(
                        PipelineEvent(
                            source="jenkins",
                            event_type="test_report",
                            timestamp=build_info.timestamp,
                            summary=(
                                f"Tests: {test_report.passed}/{test_report.total} passed "
                                f"({test_report.pass_rate:.0f}%)"
                            ),
                            url=f"{url}/testReport",
                            metadata=test_report.to_dict(),
                        )
                    )

                # Parse console errors
                console_errors = self._jenkins.parse_console_errors(url)
                if console_errors and console_errors.error_lines:
                    build_dict["console_errors"] = console_errors.to_dict()

            except Exception as exc:
                logger.warning("Jenkins correlation failed for %s: %s", url, exc)

    # TestRail correlation 

    def _correlate_testrail(
        self,
        result: PipelineCorrelation,
        issue_key: str,
    ) -> None:
        """Find TestRail runs matching build versions or issue key."""
        if not self._testrail or not getattr(self._testrail, "enabled", False):
            return

        # Strategy 1: search by issue key as marker
        try:
            runs = self._testrail.get_runs(limit=10)
            for run in runs[:5]:
                run_id = run.get("id")
                run_name = run.get("name", "")

                # Check if any build version/branch matches the run name
                match = False
                for build in result.jenkins_builds:
                    job_name = build.get("job_name", "")
                    if job_name and job_name.lower() in run_name.lower():
                        match = True
                        break
                    commit = build.get("commit_sha", "")
                    if commit and commit in run_name:
                        match = True
                        break

                # Also check if the issue key appears in refs
                marker_results = self._testrail.get_tests_by_marker(
                    run_id, issue_key, limit=10
                )

                if match or marker_results:
                    try:
                        summary = self._testrail.get_run_summary(run_id)
                        result.testrail_runs.append(summary)
                        result.timeline.append(
                            PipelineEvent(
                                source="testrail",
                                event_type="test_run",
                                summary=(
                                    f"TestRail: {summary.get('name', '')} "
                                    f"— {summary.get('pass_rate', 0)}% pass"
                                ),
                                url=summary.get("url", ""),
                                metadata=summary,
                            )
                        )
                    except Exception as exc:
                        logger.debug(
                            "TestRail run %d summary failed: %s", run_id, exc
                        )
        except Exception as exc:
            logger.warning("TestRail correlation failed: %s", exc)

    #  Confluence correlation 

    def _correlate_confluence(
        self,
        result: PipelineCorrelation,
        issue_key: str,
        issue_data: dict[str, Any],
    ) -> None:
        """Search Confluence for pages mentioning the issue key or components."""
        if not self._confluence or not getattr(self._confluence, "enabled", False):
            return

        fields = issue_data.get("fields", {})
        summary = fields.get("summary", "")
        components = [
            c.get("name", "") for c in fields.get("components", [])
        ]

        search_queries = [issue_key]
        if components:
            search_queries.append(components[0])

        for query in search_queries[:2]:
            try:
                citations = self._confluence.search_by_text(query, limit=3)
                for cite in citations:
                    cite_dict = cite.to_citation_dict()
                    # Deduplicate by page_id
                    if not any(
                        c.get("page_id") == cite.page_id
                        for c in result.confluence_citations
                    ):
                        cite_dict["page_id"] = cite.page_id
                        result.confluence_citations.append(cite_dict)
                        result.timeline.append(
                            PipelineEvent(
                                source="confluence",
                                event_type="doc_reference",
                                summary=f"Confluence: {cite.title}",
                                url=cite.url,
                                metadata=cite_dict,
                            )
                        )
            except Exception as exc:
                logger.debug("Confluence search for %r failed: %s", query, exc)
