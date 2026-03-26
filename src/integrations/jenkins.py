"""Jenkins CI client — build info, test reports, console errors."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.config import settings

logger = logging.getLogger(__name__)


# Data models

@dataclass
class JenkinsArtifact:
    """A build artifact from Jenkins."""

    filename: str
    relative_path: str
    download_url: str
    content: Optional[bytes] = None
    size: int = 0

    def as_text(self, encoding: str = "utf-8") -> str:
        """Decode content as UTF-8 text."""
        if self.content is None:
            return ""
        return self.content.decode(encoding, errors="replace")


@dataclass
class JUnitTestCase:
    """A single test case parsed from a JUnit XML report."""

    classname: str
    name: str
    time: float = 0.0
    status: str = "passed"   # passed | failed | error | skipped
    message: str = ""        # failure/error message
    stacktrace: str = ""     # full stacktrace text


@dataclass
class JUnitReport:
    """Aggregated JUnit report parsed from one or more XML files."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    time: float = 0.0
    test_cases: list[JUnitTestCase] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total > 0 else 0.0

    @property
    def failed_cases(self) -> list[JUnitTestCase]:
        return [tc for tc in self.test_cases if tc.status in ("failed", "error")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "pass_rate": round(self.pass_rate, 1),
            "time_seconds": round(self.time, 2),
            "failed_cases": [
                {
                    "classname": tc.classname,
                    "name": tc.name,
                    "status": tc.status,
                    "message": tc.message[:300],
                }
                for tc in self.failed_cases[:20]
            ],
        }


@dataclass
class ConsoleErrors:
    """Structured error lines from Jenkins console output."""

    error_lines: list[str] = field(default_factory=list)
    warning_lines: list[str] = field(default_factory=list)
    exception_blocks: list[str] = field(default_factory=list)
    total_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_count": len(self.error_lines),
            "warning_count": len(self.warning_lines),
            "exception_count": len(self.exception_blocks),
            "total_lines": self.total_lines,
            "errors": self.error_lines[:20],
            "warnings": self.warning_lines[:10],
            "exceptions": self.exception_blocks[:5],
        }


@dataclass
class JenkinsBuildInfo:
    """Complete build metadata from Jenkins JSON API."""

    build_number: int
    job_name: str
    result: str              # SUCCESS | FAILURE | UNSTABLE | ABORTED | None
    url: str
    timestamp: Optional[str] = None
    duration_ms: int = 0
    commit_sha: str = ""
    branch: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    artifacts: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_number": self.build_number,
            "job_name": self.job_name,
            "result": self.result,
            "url": self.url,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "parameters": self.parameters,
            "artifact_count": len(self.artifacts),
        }


# Client

class JenkinsClient:
    """Jenkins REST API client for build artifact retrieval and parsing."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
    ) -> None:
        self._base_url = (
            base_url or settings.log_lookup.jenkins_base_url
        ).rstrip("/")
        self._username = username or settings.log_lookup.jenkins_username
        self._api_token = api_token or settings.log_lookup.jenkins_api_token
        self._session = requests.Session()
        if self._username and self._api_token:
            self._session.auth = (self._username, self._api_token)

        if self.enabled:
            logger.info("JenkinsClient ready (%s)", self._base_url)

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._username and self._api_token)

    # Build info

    def get_build_info(self, job_url: str) -> Optional[JenkinsBuildInfo]:
        """Fetch full build metadata from a Jenkins build URL."""
        api_url = self._normalise_api_url(job_url)
        try:
            data = self._get_json(api_url)
            return self._parse_build_info(data, job_url)
        except Exception as exc:
            logger.warning("Failed to fetch build info from %s: %s", job_url, exc)
            return None

    def get_last_build(self, job_path: str) -> Optional[JenkinsBuildInfo]:
        """Fetch the last build of a job by its path (e.g. ``job/myapp``)."""
        url = f"{self._base_url}/{job_path.strip('/')}/lastBuild/api/json"
        try:
            data = self._get_json(url)
            build_url = data.get("url", "")
            return self._parse_build_info(data, build_url)
        except Exception as exc:
            logger.warning("Failed to fetch last build for %s: %s", job_path, exc)
            return None

    # Artifact listing & download

    def list_artifacts(self, job_url: str) -> list[dict[str, str]]:
        """List all artifacts for a build. Returns [{filename, relativePath, url}]."""
        api_url = self._normalise_api_url(job_url)
        try:
            data = self._get_json(api_url)
            base = data.get("url", job_url.rstrip("/"))
            return [
                {
                    "filename": a.get("fileName", ""),
                    "relativePath": a.get("relativePath", ""),
                    "url": f"{base}artifact/{a.get('relativePath', '')}",
                }
                for a in data.get("artifacts", [])
            ]
        except Exception as exc:
            logger.warning("Failed to list artifacts from %s: %s", job_url, exc)
            return []

    def download_artifact(
        self,
        artifact_url: str,
        timeout: int = 30,
    ) -> Optional[JenkinsArtifact]:
        """Download a single build artifact by URL."""
        try:
            resp = self._session.get(artifact_url, timeout=timeout)
            resp.raise_for_status()
            filename = artifact_url.rstrip("/").split("/")[-1]
            return JenkinsArtifact(
                filename=filename,
                relative_path=filename,
                download_url=artifact_url,
                content=resp.content,
                size=len(resp.content),
            )
        except Exception as exc:
            logger.warning("Artifact download failed (%s): %s", artifact_url, exc)
            return None

    def fetch_test_reports(self, job_url: str) -> list[JenkinsArtifact]:
        """Download all test report artifacts (``*.xml`` files) from a build."""
        artifacts = self.list_artifacts(job_url)
        xml_artifacts = [
            a for a in artifacts
            if a["filename"].lower().endswith(".xml")
            and any(
                kw in a["relativePath"].lower()
                for kw in ("test", "junit", "surefire", "report")
            )
        ]
        reports: list[JenkinsArtifact] = []
        for a in xml_artifacts[:10]:
            downloaded = self.download_artifact(a["url"])
            if downloaded:
                reports.append(downloaded)
        return reports

    # JUnit XML parsing

    def parse_test_report(self, job_url: str) -> Optional[JUnitReport]:
        """Download JUnit XML reports from a build and parse into a unified report."""
        xml_artifacts = self.fetch_test_reports(job_url)
        if not xml_artifacts:
            # Fallback: try the built-in testReport API
            return self._fetch_test_report_api(job_url)

        report = JUnitReport()
        for artifact in xml_artifacts:
            self._merge_junit_xml(report, artifact.as_text())
        return report if report.total > 0 else None

    @staticmethod
    def _merge_junit_xml(report: JUnitReport, xml_text: str) -> None:
        """Parse JUnit XML and merge results into *report*."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.debug("JUnit XML parse failed: %s", exc)
            return

        # Handle both <testsuite> and <testsuites> roots
        suites: list[ET.Element] = []
        if root.tag == "testsuites":
            suites = list(root)
        elif root.tag == "testsuite":
            suites = [root]
        else:
            return

        for suite in suites:
            report.time += float(suite.get("time", "0") or "0")
            for tc_elem in suite.iter("testcase"):
                tc = JUnitTestCase(
                    classname=tc_elem.get("classname", ""),
                    name=tc_elem.get("name", ""),
                    time=float(tc_elem.get("time", "0") or "0"),
                )
                failure = tc_elem.find("failure")
                error = tc_elem.find("error")
                skipped_elem = tc_elem.find("skipped")

                if failure is not None:
                    tc.status = "failed"
                    tc.message = failure.get("message", "")
                    tc.stacktrace = failure.text or ""
                    report.failed += 1
                elif error is not None:
                    tc.status = "error"
                    tc.message = error.get("message", "")
                    tc.stacktrace = error.text or ""
                    report.errors += 1
                elif skipped_elem is not None:
                    tc.status = "skipped"
                    tc.message = skipped_elem.get("message", "")
                    report.skipped += 1
                else:
                    tc.status = "passed"
                    report.passed += 1

                report.total += 1
                report.test_cases.append(tc)

    def _fetch_test_report_api(self, job_url: str) -> Optional[JUnitReport]:
        """Fallback: use Jenkins ``testReport`` API endpoint."""
        url = f"{job_url.rstrip('/')}/testReport/api/json"
        try:
            data = self._get_json(url)
            report = JUnitReport(
                total=data.get("totalCount", 0),
                failed=data.get("failCount", 0),
                skipped=data.get("skipCount", 0),
                passed=data.get("passCount", 0),
            )
            # Parse individual failed cases
            for suite in data.get("suites", []):
                for case in suite.get("cases", []):
                    status_raw = (case.get("status") or "PASSED").upper()
                    if status_raw in ("FAILED", "REGRESSION"):
                        report.test_cases.append(
                            JUnitTestCase(
                                classname=case.get("className", ""),
                                name=case.get("name", ""),
                                time=case.get("duration", 0.0),
                                status="failed",
                                message=case.get("errorDetails", "")[:500],
                                stacktrace=case.get("errorStackTrace", "")[:1000],
                            )
                        )
            return report if report.total > 0 else None
        except Exception as exc:
            logger.debug("Jenkins testReport API failed for %s: %s", job_url, exc)
            return None

    # Console output analysis

    def parse_console_errors(
        self,
        job_url: str,
        max_lines: int = 5000,
    ) -> Optional[ConsoleErrors]:
        """Fetch console output and extract error/warning/exception lines."""
        console_url = self._normalise_console_url(job_url)
        try:
            resp = self._session.get(console_url, timeout=30)
            resp.raise_for_status()
            return self._extract_errors(resp.text, max_lines)
        except Exception as exc:
            logger.warning("Console error parse failed for %s: %s", job_url, exc)
            return None

    @staticmethod
    def _extract_errors(text: str, max_lines: int = 5000) -> ConsoleErrors:
        """Parse console text for errors, warnings, and exception blocks."""
        lines = text.splitlines()[-max_lines:]
        result = ConsoleErrors(total_lines=len(lines))

        _ERROR_PATS = [
            re.compile(r"\b(ERROR|FATAL|FAILURE|BUILD FAILED)\b", re.IGNORECASE),
            re.compile(r"^\[ERROR\]", re.IGNORECASE),
            re.compile(r"(?:Exception|Error):\s+\S+", re.IGNORECASE),
        ]
        _WARN_PATS = [
            re.compile(r"\b(WARN|WARNING)\b", re.IGNORECASE),
            re.compile(r"^\[WARNING\]", re.IGNORECASE),
        ]
        _EXCEPTION_START = re.compile(
            r"^(Caused by:|.*Exception.*:|.*Error.*:|\s+at\s+\S)", re.IGNORECASE
        )

        current_exception: list[str] = []
        in_exception = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if in_exception and current_exception:
                    result.exception_blocks.append("\n".join(current_exception))
                    current_exception = []
                    in_exception = False
                continue

            # Exception block tracking
            if _EXCEPTION_START.match(stripped):
                in_exception = True
                current_exception.append(stripped)
                continue
            elif in_exception:
                if stripped.startswith("at ") or stripped.startswith("..."):
                    current_exception.append(stripped)
                    continue
                else:
                    if current_exception:
                        result.exception_blocks.append("\n".join(current_exception))
                        current_exception = []
                    in_exception = False

            # Error lines
            if any(p.search(stripped) for p in _ERROR_PATS):
                result.error_lines.append(stripped)
            # Warning lines
            elif any(p.search(stripped) for p in _WARN_PATS):
                result.warning_lines.append(stripped)

        # Flush any remaining exception block
        if current_exception:
            result.exception_blocks.append("\n".join(current_exception))

        return result

    # Helpers

    def _get_json(self, url: str) -> dict:
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _parse_build_info(self, data: dict, url: str) -> JenkinsBuildInfo:
        commit = self._extract_commit(data)
        branch = self._extract_branch(data)
        params = self._extract_parameters(data)
        ts = None
        if data.get("timestamp"):
            ts = datetime.fromtimestamp(
                data["timestamp"] / 1000, tz=timezone.utc
            ).isoformat()

        return JenkinsBuildInfo(
            build_number=data.get("number", 0),
            job_name=data.get("fullDisplayName", data.get("displayName", "")),
            result=data.get("result") or "IN_PROGRESS",
            url=data.get("url", url),
            timestamp=ts,
            duration_ms=data.get("duration", 0),
            commit_sha=commit,
            branch=branch,
            parameters=params,
            artifacts=[
                {
                    "filename": a.get("fileName", ""),
                    "relativePath": a.get("relativePath", ""),
                }
                for a in data.get("artifacts", [])
            ],
        )

    @staticmethod
    def _extract_commit(data: dict) -> str:
        for cs in data.get("changeSets", []):
            for item in cs.get("items", []):
                if item.get("commitId"):
                    return item["commitId"][:12]
        for action in data.get("actions", []):
            for param in action.get("parameters", []):
                name = (param.get("name") or "").lower()
                if name in ("git_commit", "commit_sha", "sha", "ghprb_actual_commit"):
                    return str(param.get("value", ""))[:12]
            # Git SCM action
            if action.get("_class", "").endswith("BuildData"):
                sha = action.get("lastBuiltRevision", {}).get("SHA1", "")
                if sha:
                    return sha[:12]
        return ""

    @staticmethod
    def _extract_branch(data: dict) -> str:
        for action in data.get("actions", []):
            if action.get("_class", "").endswith("BuildData"):
                branches = action.get("lastBuiltRevision", {}).get("branch", [])
                if branches:
                    return branches[0].get("name", "").replace("refs/remotes/origin/", "")
            for param in action.get("parameters", []):
                name = (param.get("name") or "").lower()
                if name in ("branch", "git_branch", "ghprb_source_branch"):
                    return str(param.get("value", ""))
        return ""

    @staticmethod
    def _extract_parameters(data: dict) -> dict[str, str]:
        params: dict[str, str] = {}
        for action in data.get("actions", []):
            for param in action.get("parameters", []):
                name = param.get("name", "")
                value = str(param.get("value", ""))
                if name:
                    params[name] = value
        return params

    @staticmethod
    def _normalise_api_url(url: str) -> str:
        url = url.rstrip("/")
        url = re.sub(r"/(console|consoleFull|consoleText|api/json)$", "", url)
        return f"{url}/api/json"

    @staticmethod
    def _normalise_console_url(url: str) -> str:
        url = url.rstrip("/")
        url = re.sub(r"/(console|consoleFull|consoleText)$", "", url)
        return f"{url}/consoleText"
