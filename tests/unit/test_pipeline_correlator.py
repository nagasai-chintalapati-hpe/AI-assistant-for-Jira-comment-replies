"""Tests for BuildPipelineCorrelator."""

import pytest
from unittest.mock import MagicMock, PropertyMock, patch
from dataclasses import dataclass

from src.agent.pipeline_correlator import (
    BuildPipelineCorrelator,
    PipelineCorrelation,
    PipelineEvent,
)


def _make_git_pr(
    pr_number=1,
    title="Fix login",
    state="merged",
    commit="abc123",
    branch="fix/login",
    url="https://github.com/org/repo/pull/1",
):
    pr = MagicMock()
    pr.pr_number = pr_number
    pr.pr_title = title
    pr.state = state
    pr.merge_commit_sha = commit
    pr.head_branch = branch
    pr.base_branch = "main"
    pr.pr_url = url
    pr.merged_at = "2026-03-17T10:00:00Z"
    pr.created_at = "2026-03-16T10:00:00Z"
    return pr


def _make_jenkins_build_info(
    number=42,
    name="Build #42",
    result="SUCCESS",
    commit="abc123",
    branch="fix/login",
):
    info = MagicMock()
    info.build_number = number
    info.job_name = name
    info.result = result
    info.url = f"https://ci/job/app/{number}/"
    info.timestamp = "2026-03-17T11:00:00Z"
    info.commit_sha = commit
    info.branch = branch
    info.duration_ms = 60000
    info.artifacts = []
    info.to_dict.return_value = {
        "build_number": number,
        "job_name": name,
        "result": result,
        "url": f"https://ci/job/app/{number}/",
        "timestamp": "2026-03-17T11:00:00Z",
        "commit_sha": commit,
        "branch": branch,
    }
    return info


ISSUE_DATA = {
    "fields": {
        "summary": "Login timeout on staging",
        "components": [{"name": "auth-service"}],
        "description": "Login fails intermittently",
    }
}


class TestCorrelatorEnabled:
    def test_enabled_with_git(self):
        git = MagicMock()
        git.enabled = True
        c = BuildPipelineCorrelator(git_client=git)
        assert c.enabled

    def test_enabled_with_jenkins(self):
        jenkins = MagicMock()
        jenkins.enabled = True
        c = BuildPipelineCorrelator(jenkins_client=jenkins)
        assert c.enabled

    def test_disabled_with_nothing(self):
        c = BuildPipelineCorrelator()
        assert not c.enabled


class TestGitCorrelation:
    def test_collects_commits_and_branches(self):
        git = MagicMock()
        git.enabled = True
        c = BuildPipelineCorrelator(git_client=git)
        prs = [_make_git_pr(), _make_git_pr(pr_number=2, commit="def456", branch="fix/search")]

        result = c.correlate("PROJ-100", ISSUE_DATA, git_prs=prs)
        assert "abc123" in result.commit_shas
        assert "def456" in result.commit_shas
        assert "fix/login" in result.branches
        assert "fix/search" in result.branches
        assert len(result.timeline) >= 2

    def test_empty_without_prs(self):
        git = MagicMock()
        git.enabled = True
        c = BuildPipelineCorrelator(git_client=git)
        result = c.correlate("PROJ-100", ISSUE_DATA, git_prs=[])
        assert result.commit_shas == []


class TestJenkinsCorrelation:
    def test_collects_build_info(self):
        jenkins = MagicMock()
        jenkins.enabled = True
        jenkins.get_build_info.return_value = _make_jenkins_build_info()
        jenkins.parse_test_report.return_value = None
        jenkins.parse_console_errors.return_value = None

        c = BuildPipelineCorrelator(jenkins_client=jenkins)
        result = c.correlate(
            "PROJ-100",
            ISSUE_DATA,
            jenkins_links=["https://ci/job/app/42/"],
        )
        assert len(result.jenkins_builds) == 1
        assert result.jenkins_builds[0]["result"] == "SUCCESS"
        assert "abc123" in result.commit_shas

    def test_includes_test_report_in_timeline(self):
        jenkins = MagicMock()
        jenkins.enabled = True
        jenkins.get_build_info.return_value = _make_jenkins_build_info()
        test_report = MagicMock()
        test_report.passed = 10
        test_report.total = 12
        test_report.pass_rate = 83.3
        test_report.to_dict.return_value = {"total": 12, "passed": 10}
        jenkins.parse_test_report.return_value = test_report
        jenkins.parse_console_errors.return_value = None

        c = BuildPipelineCorrelator(jenkins_client=jenkins)
        result = c.correlate(
            "PROJ-100",
            ISSUE_DATA,
            jenkins_links=["https://ci/job/app/42/"],
        )
        test_events = [e for e in result.timeline if e.event_type == "test_report"]
        assert len(test_events) == 1
        assert "10/12" in test_events[0].summary

    def test_skipped_when_disabled(self):
        jenkins = MagicMock()
        jenkins.enabled = False
        c = BuildPipelineCorrelator(jenkins_client=jenkins)
        result = c.correlate("PROJ-100", ISSUE_DATA, jenkins_links=["http://ci/1"])
        assert result.jenkins_builds == []


class TestTestrailCorrelation:
    def test_finds_runs_matching_issue_key(self):
        testrail = MagicMock()
        testrail.enabled = True
        testrail.get_runs.return_value = [
            {"id": 100, "name": "Build 42"},
        ]
        testrail.get_tests_by_marker.return_value = [
            {"id": 1, "title": "Test with PROJ-100", "refs": "PROJ-100"},
        ]
        testrail.get_run_summary.return_value = {
            "run_id": 100,
            "name": "Build 42",
            "pass_rate": 90.0,
            "url": "https://testrail/runs/100",
        }

        c = BuildPipelineCorrelator(testrail_client=testrail)
        result = c.correlate("PROJ-100", ISSUE_DATA)
        assert len(result.testrail_runs) == 1
        assert result.testrail_runs[0]["pass_rate"] == 90.0


class TestConfluenceCorrelation:
    def test_finds_docs_by_issue_key(self):
        confluence = MagicMock()
        confluence.enabled = True
        cite = MagicMock()
        cite.page_id = "123"
        cite.title = "Auth Runbook"
        cite.url = "https://wiki/123"
        cite.to_citation_dict.return_value = {
            "source": "Confluence: Auth Runbook",
            "url": "https://wiki/123",
            "excerpt": "Login troubleshooting steps...",
        }
        confluence.search_by_text.return_value = [cite]

        c = BuildPipelineCorrelator(confluence_client=confluence)
        result = c.correlate("PROJ-100", ISSUE_DATA)
        assert len(result.confluence_citations) == 1
        assert "Auth Runbook" in result.confluence_citations[0]["source"]


class TestPipelineCorrelation:
    def test_has_data(self):
        pc = PipelineCorrelation(issue_key="X-1", commit_shas=["abc"])
        assert pc.has_data

    def test_no_data(self):
        pc = PipelineCorrelation(issue_key="X-1")
        assert not pc.has_data

    def test_to_dict(self):
        pc = PipelineCorrelation(
            issue_key="X-1",
            commit_shas=["abc", "def"],
            jenkins_builds=[{"result": "SUCCESS"}],
            testrail_runs=[{"pass_rate": 90}],
        )
        d = pc.to_dict()
        assert d["issue_key"] == "X-1"
        assert d["jenkins_build_count"] == 1
        assert d["testrail_run_count"] == 1

    def test_to_evidence_list(self):
        pc = PipelineCorrelation(
            issue_key="X-1",
            jenkins_builds=[{"job_name": "Build 42", "result": "SUCCESS"}],
            testrail_runs=[{"name": "Run 1", "pass_rate": 95}],
            confluence_citations=[{"source": "Confluence: Doc"}],
        )
        evidence = pc.to_evidence_list()
        assert len(evidence) == 3
        assert any("Jenkins" in e for e in evidence)
        assert any("TestRail" in e for e in evidence)
        assert any("Confluence" in e for e in evidence)

    def test_timeline_sorted(self):
        git = MagicMock()
        git.enabled = True
        c = BuildPipelineCorrelator(git_client=git)
        prs = [
            _make_git_pr(pr_number=1, commit="a"),
            _make_git_pr(pr_number=2, commit="b"),
        ]
        prs[0].merged_at = "2026-03-17T10:00:00Z"
        prs[1].merged_at = "2026-03-17T12:00:00Z"
        result = c.correlate("X-1", ISSUE_DATA, git_prs=prs)
        timestamps = [e.timestamp for e in result.timeline if e.timestamp]
        assert timestamps == sorted(timestamps, reverse=True)
