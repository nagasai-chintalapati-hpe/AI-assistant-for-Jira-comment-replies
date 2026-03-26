"""Tests for TestRail by-marker retrieval."""

import pytest
from unittest.mock import patch, MagicMock

from src.integrations.testrail import TestRailClient, STATUS_MAP


def _make_client():
    with patch("src.integrations.testrail.settings") as ms:
        ms.testrail.base_url = "https://testrail.example.com"
        ms.testrail.username = "qa@co.com"
        ms.testrail.api_key = "key"
        ms.testrail.session_cookie = ""
        ms.testrail.project_id = 1
        ms.testrail.suite_id = 0
        return TestRailClient()


class TestGetTestsByMarker:
    """get_tests_by_marker filters by refs/title/custom_automation_type."""

    def test_matches_refs_field(self):
        client = _make_client()
        client.get_tests = MagicMock(return_value=[
            {"id": 1, "title": "Login test", "refs": "PROJ-100,smoke", "status_id": 1},
            {"id": 2, "title": "Logout test", "refs": "regression", "status_id": 5},
            {"id": 3, "title": "Search test", "refs": "PROJ-100,regression", "status_id": 1},
        ])
        result = client.get_tests_by_marker(run_id=99, marker="PROJ-100")
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 3

    def test_matches_title_field(self):
        client = _make_client()
        client.get_tests = MagicMock(return_value=[
            {"id": 1, "title": "PROJ-200 login flow", "refs": "", "status_id": 1},
            {"id": 2, "title": "Other test", "refs": "", "status_id": 1},
        ])
        result = client.get_tests_by_marker(run_id=99, marker="PROJ-200")
        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_matches_custom_automation_type(self):
        client = _make_client()
        client.get_tests = MagicMock(return_value=[
            {"id": 1, "title": "Test A", "refs": "", "custom_automation_type": "smoke", "status_id": 1},
            {"id": 2, "title": "Test B", "refs": "", "custom_automation_type": "regression", "status_id": 1},
        ])
        result = client.get_tests_by_marker(run_id=99, marker="smoke")
        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_case_insensitive(self):
        client = _make_client()
        client.get_tests = MagicMock(return_value=[
            {"id": 1, "title": "Test", "refs": "SMOKE,P1", "status_id": 1},
        ])
        result = client.get_tests_by_marker(run_id=99, marker="smoke")
        assert len(result) == 1

    def test_empty_when_no_match(self):
        client = _make_client()
        client.get_tests = MagicMock(return_value=[
            {"id": 1, "title": "Test", "refs": "regression", "status_id": 1},
        ])
        result = client.get_tests_by_marker(run_id=99, marker="nonexistent")
        assert result == []

    def test_status_filter_passed_through(self):
        client = _make_client()
        client.get_tests = MagicMock(return_value=[])
        client.get_tests_by_marker(run_id=99, marker="smoke", status_id="4,5")
        client.get_tests.assert_called_once_with(99, status_id="4,5", limit=250)


class TestGetResultsByMarker:
    """get_results_by_marker chains test lookup + results."""

    def test_returns_results_with_test_metadata(self):
        client = _make_client()
        client.get_tests_by_marker = MagicMock(return_value=[
            {"id": 10, "title": "Login test", "refs": "smoke", "case_id": 100},
        ])
        client.get_results_for_test = MagicMock(return_value=[
            {"id": 500, "status_id": 5, "comment": "Failed on staging"},
        ])
        results = client.get_results_by_marker(run_id=99, marker="smoke")
        assert len(results) == 1
        assert results[0]["_test_title"] == "Login test"
        assert results[0]["_test_refs"] == "smoke"
        assert results[0]["_case_id"] == 100

    def test_limits_tests_fetched(self):
        tests = [{
            "id": i + 1, "title": f"T{i}", "refs": "m", "case_id": i
        } for i in range(100)]
        client = _make_client()
        client.get_tests_by_marker = MagicMock(return_value=tests)
        client.get_results_for_test = MagicMock(return_value=[])
        client.get_results_by_marker(run_id=99, marker="m", limit=5)
        assert client.get_results_for_test.call_count == 5


class TestGetRunSummaryByMarker:
    """get_run_summary_by_marker returns marker-scoped summary."""

    def test_correct_pass_rate(self):
        client = _make_client()
        client.get_run = MagicMock(return_value={
            "name": "Build 42", "url": "https://testrail.example.com/runs/view/99"
        })
        client.get_tests_by_marker = MagicMock(return_value=[
            {"id": 1, "status_id": 1, "title": "T1", "case_id": 1, "refs": "m"},
            {"id": 2, "status_id": 1, "title": "T2", "case_id": 2, "refs": "m"},
            {"id": 3, "status_id": 5, "title": "T3", "case_id": 3, "refs": "m"},
            {"id": 4, "status_id": 4, "title": "T4", "case_id": 4, "refs": "m"},
        ])
        summary = client.get_run_summary_by_marker(run_id=99, marker="m")
        assert summary["marker"] == "m"
        assert summary["total"] == 4
        assert summary["passed"] == 2
        assert summary["failed"] == 1
        assert summary["retest"] == 1
        assert summary["pass_rate"] == 50.0
        assert len(summary["failed_tests"]) == 2  # failed + retest

    def test_empty_marker_result(self):
        client = _make_client()
        client.get_run = MagicMock(return_value={"name": "Run"})
        client.get_tests_by_marker = MagicMock(return_value=[])
        summary = client.get_run_summary_by_marker(run_id=99, marker="nothing")
        assert summary["total"] == 0
        assert summary["pass_rate"] == 0.0
