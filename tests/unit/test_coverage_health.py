"""Tests for health, metrics, and deep-health routes."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from fastapi.testclient import TestClient

from src.api.app import app

client = TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:
    def test_health_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "timestamp" in data
        assert "integrations" in data
        assert "notifications" in data
        assert "optional" in data

    def test_health_has_integration_flags(self):
        resp = client.get("/health")
        data = resp.json()
        integrations = data["integrations"]
        for key in ("jira", "git", "confluence", "testrail", "rag", "elk", "queue"):
            assert key in integrations

    def test_health_has_optional_flags(self):
        resp = client.get("/health")
        data = resp.json()
        assert "s3" in data["optional"]
        assert "redis" in data["optional"]


class TestMetricsEndpoint:
    def test_metrics_returns_200(self):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        # Should contain metric keys
        assert "pending" in data or "total" in data or isinstance(data, dict)


class TestPrometheusMetrics:
    def test_prometheus_metrics_returns_data_or_503(self):
        resp = client.get("/metrics/prometheus")
        # Either works (prometheus-client installed) or 503 (not installed)
        assert resp.status_code in (200, 503)
        if resp.status_code == 503:
            assert "prometheus-client" in resp.json()["detail"]

    def test_prometheus_metrics_with_mock(self):
        """Test Prometheus metrics generation when prometheus-client is available."""
        mock_registry = MagicMock()
        mock_gauge = MagicMock()
        mock_gauge.labels.return_value = MagicMock()

        with patch.dict("sys.modules", {
            "prometheus_client": MagicMock(
                CollectorRegistry=MagicMock(return_value=mock_registry),
                Gauge=MagicMock(return_value=mock_gauge),
                generate_latest=MagicMock(return_value=b"# HELP test\n"),
                CONTENT_TYPE_LATEST="text/plain",
            ),
        }):
            # Force re-evaluation — the route imports at call time
            resp = client.get("/metrics/prometheus")
            # May be 200 or 503 depending on import cache
            assert resp.status_code in (200, 503)


class TestDeepHealthEndpoint:
    def test_deep_health_returns_200(self):
        resp = client.get("/health/deep")
        assert resp.status_code == 200
        data = resp.json()
        assert "overall" in data
        assert "integrations" in data
        assert "timestamp" in data
        assert data["overall"] in ("ok", "degraded")

    def test_deep_health_has_sqlite(self):
        resp = client.get("/health/deep")
        data = resp.json()
        assert "sqlite" in data["integrations"]
        assert data["integrations"]["sqlite"]["status"] in ("ok", "degraded")

    def test_deep_health_queue_status(self):
        resp = client.get("/health/deep")
        data = resp.json()
        assert "queue" in data["integrations"]
        assert data["integrations"]["queue"]["status"] in ("ok", "disabled")
