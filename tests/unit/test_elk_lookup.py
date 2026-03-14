"""Unit tests for LogLookupService — ELK / OpenSearch backend."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.integrations.log_lookup import LogLookupService
from src.models.rag import LogEntry


# ── Fixtures ──────────────────────────────────────────────────────────────────

ELK_RESPONSE = {
    "hits": {
        "total": {"value": 2, "relation": "eq"},
        "hits": [
            {
                "_index": "logs-2026.02.10",
                "_id": "abc1",
                "_source": {
                    "@timestamp": "2026-02-10T14:12:00Z",
                    "message": "SnapshotLockTimeout: acquire failed after 30s",
                    "level": "ERROR",
                    "build_id": "1.8.14",
                    "environment": "staging",
                    "correlation_id": "req-9999",
                    "service": {"name": "snapshot-service"},
                },
            },
            {
                "_index": "logs-2026.02.10",
                "_id": "abc2",
                "_source": {
                    "@timestamp": "2026-02-10T14:13:00Z",
                    "message": "POST /snapshot returned 500",
                    "log": {"level": "WARN", "message": "API error logged"},
                    "correlation_id": "req-9999",
                },
            },
        ],
    }
}

ELK_EMPTY_RESPONSE = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}


def _make_service(elk_host="http://elk.internal:9200", username="admin", password="secret"):
    return LogLookupService(
        jenkins_base_url="",
        elk_host=elk_host,
        elk_username=username,
        elk_password=password,
    )


# ── elk_enabled ───────────────────────────────────────────────────────────────

def test_elk_enabled_with_basic_auth():
    svc = _make_service()
    assert svc.elk_enabled is True


def test_elk_enabled_with_api_key():
    svc = LogLookupService(elk_host="http://elk:9200", elk_api_key="key-xyz")
    assert svc.elk_enabled is True


def test_elk_disabled_when_no_host():
    svc = LogLookupService(elk_host="", elk_username="admin", elk_password="secret")
    assert svc.elk_enabled is False


def test_elk_disabled_when_no_auth():
    svc = LogLookupService(elk_host="http://elk:9200")
    assert svc.elk_enabled is False


# ── search_elk_logs — happy path ──────────────────────────────────────────────

def test_search_elk_logs_returns_entries():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_RESPONSE
    mock_resp.raise_for_status.return_value = None

    with patch("src.integrations.log_lookup.requests.post", return_value=mock_resp):
        entries = svc.search_elk_logs("SnapshotLockTimeout")

    assert len(entries) == 2
    assert all(isinstance(e, LogEntry) for e in entries)


def test_search_elk_logs_first_entry_fields():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_RESPONSE
    mock_resp.raise_for_status.return_value = None

    with patch("src.integrations.log_lookup.requests.post", return_value=mock_resp):
        entries = svc.search_elk_logs("SnapshotLockTimeout")

    e = entries[0]
    assert e.source == "elk"
    assert e.level == "ERROR"
    assert "SnapshotLockTimeout" in e.message
    assert e.timestamp == "2026-02-10T14:12:00Z"
    assert e.correlation_id == "req-9999"
    assert e.context is not None
    assert e.context.get("build_id") == "1.8.14"


def test_search_elk_logs_second_entry_uses_log_message():
    """When top-level message is absent, fall back to log.message."""
    svc = _make_service()
    response = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "@timestamp": "2026-02-10T14:13:00Z",
                        "log": {"level": "WARN", "message": "API error logged"},
                        "correlation_id": "req-0001",
                    }
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = response
    mock_resp.raise_for_status.return_value = None

    with patch("src.integrations.log_lookup.requests.post", return_value=mock_resp):
        entries = svc.search_elk_logs("API error")

    assert len(entries) == 1
    assert entries[0].message == "API error logged"
    assert entries[0].level == "WARN"


def test_search_elk_logs_empty_result():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_EMPTY_RESPONSE
    mock_resp.raise_for_status.return_value = None

    with patch("src.integrations.log_lookup.requests.post", return_value=mock_resp):
        entries = svc.search_elk_logs("nonexistent error")

    assert entries == []


# ── search_elk_logs — disabled ────────────────────────────────────────────────

def test_search_elk_logs_disabled_returns_empty():
    svc = LogLookupService(elk_host="", elk_username="admin", elk_password="secret")
    entries = svc.search_elk_logs("anything")
    assert entries == []


# ── search_elk_logs — filters ─────────────────────────────────────────────────

def test_search_elk_logs_passes_build_id_filter():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_EMPTY_RESPONSE
    mock_resp.raise_for_status.return_value = None

    captured_body = {}

    def capture_post(url, json=None, **kwargs):
        captured_body.update(json or {})
        return mock_resp

    with patch("src.integrations.log_lookup.requests.post", side_effect=capture_post):
        svc.search_elk_logs("error", build_id="1.8.14")

    must_clauses = captured_body["query"]["bool"]["must"]
    assert any("term" in c and c["term"].get("build_id") == "1.8.14" for c in must_clauses)


def test_search_elk_logs_passes_env_filter():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_EMPTY_RESPONSE
    mock_resp.raise_for_status.return_value = None

    captured_body = {}

    def capture_post(url, json=None, **kwargs):
        captured_body.update(json or {})
        return mock_resp

    with patch("src.integrations.log_lookup.requests.post", side_effect=capture_post):
        svc.search_elk_logs("error", env="staging")

    must_clauses = captured_body["query"]["bool"]["must"]
    # env filter is a bool/should wrapping two term queries
    env_filter = [c for c in must_clauses if "bool" in c]
    assert len(env_filter) > 0


def test_search_elk_logs_passes_correlation_id_filter():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_EMPTY_RESPONSE
    mock_resp.raise_for_status.return_value = None

    captured_body = {}

    def capture_post(url, json=None, **kwargs):
        captured_body.update(json or {})
        return mock_resp

    with patch("src.integrations.log_lookup.requests.post", side_effect=capture_post):
        svc.search_elk_logs("timeout", correlation_id="req-9999")

    must_clauses = captured_body["query"]["bool"]["must"]
    bool_filters = [c for c in must_clauses if "bool" in c]
    # correlation_id produces a bool/should with correlation_id, request_id, trace_id
    flat_should_terms = []
    for bf in bool_filters:
        for sh in bf.get("bool", {}).get("should", []):
            flat_should_terms.extend(sh.get("term", {}).keys())
    assert "correlation_id" in flat_should_terms


def test_search_elk_logs_custom_time_window():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_EMPTY_RESPONSE
    mock_resp.raise_for_status.return_value = None

    captured_body = {}

    def capture_post(url, json=None, **kwargs):
        captured_body.update(json or {})
        return mock_resp

    with patch("src.integrations.log_lookup.requests.post", side_effect=capture_post):
        svc.search_elk_logs("error", time_window_hours=48)

    must_clauses = captured_body["query"]["bool"]["must"]
    range_clauses = [c for c in must_clauses if "range" in c]
    assert len(range_clauses) == 1
    assert range_clauses[0]["range"]["@timestamp"]["gte"] == "now-48h"


def test_search_elk_logs_custom_max_entries():
    svc = _make_service()
    mock_resp = MagicMock()
    mock_resp.json.return_value = ELK_EMPTY_RESPONSE
    mock_resp.raise_for_status.return_value = None

    captured_body = {}

    def capture_post(url, json=None, **kwargs):
        captured_body.update(json or {})
        return mock_resp

    with patch("src.integrations.log_lookup.requests.post", side_effect=capture_post):
        svc.search_elk_logs("error", max_entries=10)

    assert captured_body["size"] == 10


# ── search_elk_logs — error handling ─────────────────────────────────────────

def test_search_elk_logs_returns_empty_on_http_error():
    svc = _make_service()

    with patch(
        "src.integrations.log_lookup.requests.post",
        side_effect=Exception("Connection refused"),
    ):
        entries = svc.search_elk_logs("error")

    assert entries == []


# ── auth headers ─────────────────────────────────────────────────────────────

def test_elk_auth_headers_basic():
    import base64
    svc = _make_service(username="admin", password="secret")
    headers = svc._elk_auth_headers()
    expected_token = base64.b64encode(b"admin:secret").decode()
    assert headers["Authorization"] == f"Basic {expected_token}"


def test_elk_auth_headers_api_key():
    svc = LogLookupService(elk_host="http://elk:9200", elk_api_key="my-api-key")
    headers = svc._elk_auth_headers()
    assert headers["Authorization"] == "ApiKey my-api-key"


def test_elk_auth_headers_api_key_takes_precedence():
    """API key should take precedence over basic auth when both are set."""
    svc = LogLookupService(
        elk_host="http://elk:9200",
        elk_api_key="my-key",
        elk_username="admin",
        elk_password="secret",
    )
    headers = svc._elk_auth_headers()
    assert headers["Authorization"].startswith("ApiKey")


def test_elk_auth_headers_empty_when_no_auth():
    svc = LogLookupService(elk_host="http://elk:9200")
    assert svc._elk_auth_headers() == {}


# ── _build_elk_query structure ────────────────────────────────────────────────

def test_build_elk_query_structure():
    query = LogLookupService._build_elk_query(
        query="SnapshotLockTimeout",
        build_id=None,
        env=None,
        correlation_id=None,
        time_window_hours=24,
        size=50,
    )
    assert query["size"] == 50
    assert "query" in query
    must = query["query"]["bool"]["must"]
    assert any("multi_match" in c for c in must)
    assert any("range" in c for c in must)
    # No optional filters when all are None
    optional = [c for c in must if "term" in c or ("bool" in c)]
    assert len(optional) == 0


def test_build_elk_query_with_all_filters():
    query = LogLookupService._build_elk_query(
        query="error",
        build_id="1.8.14",
        env="staging",
        correlation_id="req-001",
        time_window_hours=12,
        size=25,
    )
    must = query["query"]["bool"]["must"]
    # Should have: multi_match + range + build_id term + env bool + correlation bool = 5
    assert len(must) == 5


# ── _parse_elk_response — edge cases ─────────────────────────────────────────

def test_parse_elk_response_skips_empty_message():
    """Hits with no message field should be skipped."""
    response = {
        "hits": {
            "hits": [
                {"_source": {"@timestamp": "2026-02-10T00:00:00Z"}},  # no message
                {"_source": {"message": "real error", "@timestamp": "2026-02-10T00:00:00Z"}},
            ]
        }
    }
    entries = LogLookupService._parse_elk_response(response)
    assert len(entries) == 1
    assert entries[0].message == "real error"


def test_parse_elk_response_handles_missing_hits():
    """Gracefully handle malformed/missing hits."""
    entries = LogLookupService._parse_elk_response({})
    assert entries == []

    entries = LogLookupService._parse_elk_response({"hits": {}})
    assert entries == []
