"""Tests for LogLookupService – mocked HTTP / filesystem."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from src.integrations.log_lookup import LogLookupService
from src.config import LogLookupConfig


# ---- helpers ----------------------------------------------------------- #

def _make_config(**overrides) -> LogLookupConfig:
    defaults = {
        "jenkins_base_url": "",
        "jenkins_username": "",
        "jenkins_api_token": "",
        "log_dir": "",
        "default_time_window_hours": 24,
    }
    defaults.update(overrides)
    return LogLookupConfig(**defaults)


# ---- tests ------------------------------------------------------------- #

class TestLogLookupInit:
    def test_default_init_disabled(self):
        svc = LogLookupService()
        assert not svc.jenkins_enabled
        assert not svc.local_enabled

    def test_jenkins_enabled_when_configured(self):
        with patch("src.integrations.log_lookup.settings") as mock_settings:
            mock_settings.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.example.com",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()
            assert svc.jenkins_enabled
            assert not svc.local_enabled

    def test_local_enabled_when_log_dir_set(self, tmp_path):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(log_dir=str(tmp_path))
            svc = LogLookupService()
            assert svc.local_enabled
            assert not svc.jenkins_enabled


class TestNormaliseConsoleUrl:
    def test_adds_consoleText_suffix(self):
        svc = LogLookupService()
        url = svc._normalise_console_url("https://jenkins.co/job/build/42")
        assert url.endswith("/consoleText")

    def test_replaces_consoleFull(self):
        svc = LogLookupService()
        url = svc._normalise_console_url(
            "https://jenkins.co/job/build/42/consoleFull"
        )
        assert url.endswith("/consoleText")
        assert "consoleFull" not in url


class TestFetchJenkinsConsole:
    def test_returns_log_entry_on_success(self):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.co",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "ERROR: Build failed\njava.lang.NullPointerException"
        fake_resp.raise_for_status = MagicMock()

        with patch("src.integrations.log_lookup.requests.get", return_value=fake_resp):
            entry = svc.fetch_jenkins_console(
                "https://jenkins.co/job/build/42", max_lines=50,
            )

        assert entry is not None
        assert entry.source == "jenkins"
        assert "NullPointerException" in entry.message

    def test_returns_none_when_disabled(self):
        svc = LogLookupService()
        entry = svc.fetch_jenkins_console("https://jenkins.co/job/build/99")
        assert entry is None

    def test_returns_none_on_http_error(self):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.co",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()

        with patch("src.integrations.log_lookup.requests.get", side_effect=Exception("timeout")):
            entry = svc.fetch_jenkins_console("https://jenkins.co/job/build/99")

        assert entry is None


class TestFetchJenkinsLogsForUrls:
    def test_collects_entries_for_valid_urls(self):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.co",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "INFO: Success\nWARN: Something odd"
        fake_resp.raise_for_status = MagicMock()

        with patch("src.integrations.log_lookup.requests.get", return_value=fake_resp):
            entries = svc.fetch_jenkins_logs_for_urls([
                "https://jenkins.co/job/a/1",
                "https://jenkins.co/job/b/2",
            ])

        assert len(entries) == 2
        assert all(e.source == "jenkins" for e in entries)

    def test_skips_failing_urls(self):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.co",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()

        call_count = 0

        def side_effect(url, **kw):
            nonlocal call_count
            call_count += 1
            if "bad" in url:
                raise Exception("Connection refused")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "OK build"
            resp.raise_for_status = MagicMock()
            return resp

        with patch("src.integrations.log_lookup.requests.get", side_effect=side_effect):
            entries = svc.fetch_jenkins_logs_for_urls([
                "https://jenkins.co/job/good/1",
                "https://jenkins.co/job/bad/2",
            ])

        assert len(entries) == 1


class TestSearchLocalLogs:
    def test_returns_empty_when_disabled(self):
        svc = LogLookupService()
        assert not svc.local_enabled
        entries = svc.search_local_logs("error")
        assert entries == []


class TestGetBuildMetadata:
    def test_extracts_metadata_on_success(self):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.co",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()

        api_data = {
            "displayName": "#10 - v2.4.0",
            "fullDisplayName": "main #10",
            "timestamp": 1700000000000,
            "changeSets": [
                {
                    "items": [
                        {"commitId": "abc1234def5678"},
                    ]
                }
            ],
        }
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = api_data
        fake_resp.raise_for_status = MagicMock()

        with patch("src.integrations.log_lookup.requests.get", return_value=fake_resp):
            metadata = svc.get_build_metadata("https://jenkins.co/job/main/10")

        assert metadata is not None
        assert metadata["commit"] == "abc1234def56"  # truncated to 12 chars
        assert "v2.4.0" in metadata["version"]

    def test_returns_none_when_disabled(self):
        svc = LogLookupService()
        metadata = svc.get_build_metadata("https://jenkins.co/job/main/10")
        assert metadata is None

    def test_returns_none_on_failure(self):
        with patch("src.integrations.log_lookup.settings") as ms:
            ms.log_lookup = _make_config(
                jenkins_base_url="https://jenkins.co",
                jenkins_username="user",
                jenkins_api_token="tok",
            )
            svc = LogLookupService()

        with patch("src.integrations.log_lookup.requests.get", side_effect=Exception("403")):
            metadata = svc.get_build_metadata("https://jenkins.co/job/main/10")

        assert metadata is None
