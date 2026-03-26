"""Tests for JenkinsClient — artifact parsing, JUnit reports, console errors."""

import pytest
from unittest.mock import patch, MagicMock

from src.integrations.jenkins import (
    JenkinsClient,
    JenkinsArtifact,
    JUnitTestCase,
    JUnitReport,
    ConsoleErrors,
    JenkinsBuildInfo,
)


def _make_client():
    with patch("src.integrations.jenkins.settings") as ms:
        ms.log_lookup.jenkins_base_url = "https://jenkins.example.com"
        ms.log_lookup.jenkins_username = "ci"
        ms.log_lookup.jenkins_api_token = "token"
        return JenkinsClient()


# JUnit XML parsing

JUNIT_XML_SIMPLE = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="tests" tests="4" failures="1" errors="1" skipped="1" time="2.5">
  <testcase classname="com.app.LoginTest" name="testLoginSuccess" time="0.5"/>
  <testcase classname="com.app.LoginTest" name="testLoginFailure" time="0.8">
    <failure message="Expected 200 but got 401">
      AssertionError at LoginTest.java:42
    </failure>
  </testcase>
  <testcase classname="com.app.SearchTest" name="testSearchCrash" time="0.3">
    <error message="NullPointerException">
      java.lang.NullPointerException at SearchTest.java:15
    </error>
  </testcase>
  <testcase classname="com.app.SearchTest" name="testSearchSkipped" time="0.0">
    <skipped message="Disabled"/>
  </testcase>
</testsuite>
"""

JUNIT_XML_TESTSUITES = """\
<?xml version="1.0"?>
<testsuites>
  <testsuite name="unit" tests="2" failures="0" time="1.0">
    <testcase classname="A" name="test1" time="0.5"/>
    <testcase classname="A" name="test2" time="0.5"/>
  </testsuite>
  <testsuite name="integration" tests="1" failures="1" time="2.0">
    <testcase classname="B" name="test3" time="2.0">
      <failure message="Timeout"/>
    </testcase>
  </testsuite>
</testsuites>
"""


class TestJUnitXMLParsing:
    def test_parse_simple_testsuite(self):
        report = JUnitReport()
        JenkinsClient._merge_junit_xml(report, JUNIT_XML_SIMPLE)
        assert report.total == 4
        assert report.passed == 1
        assert report.failed == 1
        assert report.errors == 1
        assert report.skipped == 1
        assert report.pass_rate == 25.0

    def test_parse_testsuites_wrapper(self):
        report = JUnitReport()
        JenkinsClient._merge_junit_xml(report, JUNIT_XML_TESTSUITES)
        assert report.total == 3
        assert report.passed == 2
        assert report.failed == 1
        assert report.pass_rate == pytest.approx(66.7, rel=0.1)

    def test_failed_cases_property(self):
        report = JUnitReport()
        JenkinsClient._merge_junit_xml(report, JUNIT_XML_SIMPLE)
        failed = report.failed_cases
        assert len(failed) == 2  # 1 failure + 1 error
        assert any(tc.name == "testLoginFailure" for tc in failed)
        assert any(tc.name == "testSearchCrash" for tc in failed)

    def test_to_dict(self):
        report = JUnitReport()
        JenkinsClient._merge_junit_xml(report, JUNIT_XML_SIMPLE)
        d = report.to_dict()
        assert d["total"] == 4
        assert d["pass_rate"] == 25.0
        assert len(d["failed_cases"]) == 2

    def test_bad_xml_graceful(self):
        report = JUnitReport()
        JenkinsClient._merge_junit_xml(report, "not xml at all")
        assert report.total == 0

    def test_merge_multiple_files(self):
        report = JUnitReport()
        JenkinsClient._merge_junit_xml(report, JUNIT_XML_SIMPLE)
        JenkinsClient._merge_junit_xml(report, JUNIT_XML_TESTSUITES)
        assert report.total == 7  # 4 + 3


# Console error extraction

CONSOLE_OUTPUT = """\
[INFO] Building project v2.3
[INFO] Compiling sources...
[WARNING] Deprecated API usage in LoginService.java
[ERROR] Failed to compile SearchModule.java:42
[ERROR] BUILD FAILED
Caused by: java.lang.NullPointerException
  at com.app.Search.init(Search.java:15)
  at com.app.App.main(App.java:5)

[INFO] Build complete
"""


class TestConsoleErrorExtraction:
    def test_extracts_errors(self):
        result = JenkinsClient._extract_errors(CONSOLE_OUTPUT)
        assert len(result.error_lines) >= 1
        assert any("BUILD FAILED" in e for e in result.error_lines)

    def test_extracts_warnings(self):
        result = JenkinsClient._extract_errors(CONSOLE_OUTPUT)
        assert len(result.warning_lines) >= 1
        assert any("Deprecated" in w for w in result.warning_lines)

    def test_extracts_exception_blocks(self):
        result = JenkinsClient._extract_errors(CONSOLE_OUTPUT)
        assert len(result.exception_blocks) >= 1
        assert any("NullPointerException" in e for e in result.exception_blocks)

    def test_to_dict(self):
        result = JenkinsClient._extract_errors(CONSOLE_OUTPUT)
        d = result.to_dict()
        assert d["error_count"] >= 1
        assert d["warning_count"] >= 1
        assert d["exception_count"] >= 1

    def test_empty_console(self):
        result = JenkinsClient._extract_errors("")
        assert result.error_lines == []
        assert result.warning_lines == []
        assert result.exception_blocks == []


# Build info parsing

class TestBuildInfoParsing:
    def test_parse_build_info(self):
        client = _make_client()
        data = {
            "number": 42,
            "fullDisplayName": "MyApp #42",
            "result": "FAILURE",
            "url": "https://jenkins.example.com/job/myapp/42/",
            "timestamp": 1709000000000,
            "duration": 120000,
            "artifacts": [
                {"fileName": "report.xml", "relativePath": "target/report.xml"}
            ],
            "changeSets": [
                {"items": [{"commitId": "abc123def456"}]}
            ],
            "actions": [
                {
                    "_class": "hudson.plugins.git.util.BuildData",
                    "lastBuiltRevision": {
                        "SHA1": "abc123def456",
                        "branch": [{"name": "refs/remotes/origin/feature/fix-login"}],
                    },
                }
            ],
        }
        info = client._parse_build_info(data, "https://jenkins.example.com/job/myapp/42/")
        assert info.build_number == 42
        assert info.result == "FAILURE"
        assert info.commit_sha == "abc123def456"
        assert "fix-login" in info.branch
        assert len(info.artifacts) == 1

    def test_to_dict(self):
        info = JenkinsBuildInfo(
            build_number=10,
            job_name="test-job",
            result="SUCCESS",
            url="http://ci/10",
        )
        d = info.to_dict()
        assert d["build_number"] == 10
        assert d["result"] == "SUCCESS"


# Artifact

class TestJenkinsArtifact:
    def test_as_text(self):
        a = JenkinsArtifact(
            filename="log.txt",
            relative_path="build/log.txt",
            download_url="http://ci/artifact/log.txt",
            content=b"Hello World\nLine 2",
        )
        assert "Hello World" in a.as_text()

    def test_as_text_empty(self):
        a = JenkinsArtifact(
            filename="empty.txt",
            relative_path="empty.txt",
            download_url="http://ci/empty.txt",
        )
        assert a.as_text() == ""


# Client properties

class TestJenkinsClientInit:
    def test_enabled(self):
        client = _make_client()
        assert client.enabled

    def test_disabled_no_url(self):
        with patch("src.integrations.jenkins.settings") as ms:
            ms.log_lookup.jenkins_base_url = ""
            ms.log_lookup.jenkins_username = "u"
            ms.log_lookup.jenkins_api_token = "t"
            client = JenkinsClient()
        assert not client.enabled

    def test_disabled_no_token(self):
        with patch("src.integrations.jenkins.settings") as ms:
            ms.log_lookup.jenkins_base_url = "http://ci"
            ms.log_lookup.jenkins_username = "u"
            ms.log_lookup.jenkins_api_token = ""
            client = JenkinsClient()
        assert not client.enabled


class TestURLNormalisation:
    def test_api_url(self):
        assert JenkinsClient._normalise_api_url(
            "https://ci/job/app/42/console"
        ) == "https://ci/job/app/42/api/json"

    def test_console_url(self):
        assert JenkinsClient._normalise_console_url(
            "https://ci/job/app/42"
        ) == "https://ci/job/app/42/consoleText"
