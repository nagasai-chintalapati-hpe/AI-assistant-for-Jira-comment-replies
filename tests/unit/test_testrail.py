"""Tests for TestRailClient – mocked HTTP calls."""

import pytest
from unittest.mock import patch, MagicMock
import json

from src.integrations.testrail import TestRailClient, STATUS_MAP


# helpers

def _make_testrail(**overrides) -> TestRailClient:
    """Build a TestRailClient with patched config."""
    defaults = {
        "base_url": "https://testrail.example.com",
        "username": "qa@co.com",
        "api_key": "test-api-key",
        "session_cookie": "",
        "project_id": 0,
        "suite_id": 0,
    }
    defaults.update(overrides)
    with patch("src.integrations.testrail.settings") as mock_settings:
        mock_settings.testrail.base_url = defaults["base_url"]
        mock_settings.testrail.username = defaults["username"]
        mock_settings.testrail.api_key = defaults["api_key"]
        mock_settings.testrail.session_cookie = defaults["session_cookie"]
        mock_settings.testrail.project_id = defaults["project_id"]
        mock_settings.testrail.suite_id = defaults["suite_id"]
        client = TestRailClient()
    return client


def _mock_response(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    if status_code >= 400:
        from requests.exceptions import HTTPError
        resp.raise_for_status.side_effect = HTTPError(response=resp)
    return resp


# tests

class TestTestRailClientInit:
    def test_enabled_with_api_key(self):
        client = _make_testrail()
        assert client.enabled
        assert client.auth_mode == "api_key"

    def test_enabled_with_session_cookie(self):
        client = _make_testrail(
            username="", api_key="", session_cookie="abc123session",
        )
        assert client.enabled
        assert client.auth_mode == "session_cookie"

    def test_disabled_when_no_url(self):
        client = _make_testrail(base_url="")
        assert not client.enabled

    def test_disabled_when_no_credentials(self):
        client = _make_testrail(username="", api_key="", session_cookie="")
        assert not client.enabled

    def test_api_key_takes_priority_over_cookie(self):
        client = _make_testrail(session_cookie="also-set")
        assert client.auth_mode == "api_key"


class TestStatusMap:
    def test_known_statuses(self):
        assert STATUS_MAP[1] == "passed"
        assert STATUS_MAP[5] == "failed"
        assert STATUS_MAP[2] == "blocked"
        assert STATUS_MAP[3] == "untested"
        assert STATUS_MAP[4] == "retest"


class TestGetRun:
    def test_returns_run_data(self):
        client = _make_testrail()
        run_data = {"id": 100, "name": "Sprint 42", "passed_count": 50, "failed_count": 2}

        with patch.object(client._session, "get", return_value=_mock_response(run_data)):
            result = client.get_run(100)

        assert result["id"] == 100
        assert result["name"] == "Sprint 42"

    def test_raises_on_error(self):
        client = _make_testrail()

        with patch.object(client._session, "get", return_value=_mock_response({}, 404)):
            with pytest.raises(Exception):
                client.get_run(999)


class TestGetRuns:
    def test_returns_list(self):
        client = _make_testrail()
        runs = {"runs": [{"id": 1}, {"id": 2}]}

        with patch.object(client._session, "get", return_value=_mock_response(runs)):
            result = client.get_runs(project_id=1)

        assert len(result) == 2

    def test_with_suite_id(self):
        client = _make_testrail()
        runs = {"runs": [{"id": 10, "suite_id": 5}]}

        with patch.object(client._session, "get", return_value=_mock_response(runs)) as mock_get:
            result = client.get_runs(project_id=1, suite_id=5)

        assert len(result) == 1
        # suite_id passed as query param
        call_kwargs = mock_get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params.get("suite_id") == 5

    def test_uses_project_id_from_config(self):
        """project_id defaults to settings.testrail.project_id when not provided."""
        client = _make_testrail(project_id=1, suite_id=5)
        runs = {"runs": [{"id": 10, "name": "Run A"}]}

        with patch.object(client._session, "get", return_value=_mock_response(runs)) as mock_get:
            with patch("src.integrations.testrail.settings") as ms:
                ms.testrail.project_id = 1
                ms.testrail.suite_id = 5
                result = client.get_runs()  # no explicit project_id

        # URL should include project_id=1
        called_url = mock_get.call_args[0][0]
        assert "get_runs/1" in called_url

    def test_raises_when_no_project_id(self):
        client = _make_testrail(project_id=0)
        with patch("src.integrations.testrail.settings") as ms:
            ms.testrail.project_id = 0
            ms.testrail.suite_id = 0
            with pytest.raises(ValueError, match="project_id"):
                client.get_runs()


class TestGetRecentRunSummary:
    def test_returns_latest_run_summary(self):
        client = _make_testrail(project_id=1, suite_id=5)
        runs_resp = {"runs": [{"id": 100, "name": "Sprint 1"}]}
        run_resp = {
            "id": 100, "name": "Sprint 1",
            "passed_count": 8, "failed_count": 0,
            "blocked_count": 0, "untested_count": 0, "retest_count": 0,
        }

        def side_effect(url, **kw):
            if "get_runs" in url:
                return _mock_response(runs_resp)
            if "get_run/" in url:
                return _mock_response(run_resp)
            return _mock_response({})

        with patch("src.integrations.testrail.settings") as ms:
            ms.testrail.project_id = 1
            ms.testrail.suite_id = 5
            with patch.object(client._session, "get", side_effect=side_effect):
                summary = client.get_recent_run_summary()

        assert summary is not None
        assert summary["run_id"] == 100
        assert summary["name"] == "Sprint 1"
        assert summary["passed"] == 8
        assert summary["pass_rate"] == 100.0

    def test_returns_none_when_not_enabled(self):
        client = _make_testrail(username="", api_key="", session_cookie="")
        assert client.get_recent_run_summary() is None

    def test_returns_none_when_no_runs(self):
        client = _make_testrail(project_id=1)
        empty = {"runs": []}

        with patch("src.integrations.testrail.settings") as ms:
            ms.testrail.project_id = 1
            ms.testrail.suite_id = 0
            with patch.object(client._session, "get", return_value=_mock_response(empty)):
                result = client.get_recent_run_summary()

        assert result is None


class TestGetTests:
    def test_returns_tests(self):
        client = _make_testrail()
        tests = {"tests": [{"id": 1, "title": "Login test", "status_id": 5}]}

        with patch.object(client._session, "get", return_value=_mock_response(tests)):
            result = client.get_tests(run_id=100)

        assert len(result) == 1
        assert result[0]["title"] == "Login test"

    def test_filter_by_status(self):
        client = _make_testrail()
        tests = {"tests": [{"id": 1, "title": "Failed test", "status_id": 5}]}

        with patch.object(client._session, "get", return_value=_mock_response(tests)) as mock_get:
            result = client.get_tests(run_id=100, status_id=5)

        # status_id is passed as a query param, not in the URL path
        call_kwargs = mock_get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params.get("status_id") == 5


class TestGetResultsForRun:
    def test_returns_results(self):
        client = _make_testrail()
        results = {"results": [{"id": 1, "test_id": 10, "status_id": 5}]}

        with patch.object(client._session, "get", return_value=_mock_response(results)):
            result = client.get_results_for_run(run_id=100)

        assert len(result) == 1


class TestGetRunSummary:
    def test_returns_summary_with_pass_rate(self):
        client = _make_testrail()
        run_data = {
            "id": 100,
            "name": "Sprint 42 Tests",
            "url": "https://testrail.example.com/index.php?/runs/view/100",
            "passed_count": 48,
            "failed_count": 2,
            "blocked_count": 0,
            "untested_count": 0,
            "retest_count": 0,
        }
        failed_tests = {"tests": [
            {"id": 1, "title": "Upload fails", "status_id": 5},
            {"id": 2, "title": "Login timeout", "status_id": 5},
        ]}

        def side_effect(url, **kw):
            if "get_run" in url:
                return _mock_response(run_data)
            elif "get_tests" in url:
                return _mock_response(failed_tests)
            return _mock_response({})

        with patch.object(client._session, "get", side_effect=side_effect):
            summary = client.get_run_summary(100)

        assert summary is not None
        assert summary["name"] == "Sprint 42 Tests"
        assert summary["pass_rate"] == 96.0 
        assert summary["failed"] == 2
        assert len(summary["failed_tests"]) == 2

    def test_raises_when_run_not_found(self):
        client = _make_testrail()

        with patch.object(client._session, "get", return_value=_mock_response({}, 404)):
            with pytest.raises(Exception):
                client.get_run_summary(999)
