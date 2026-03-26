"""Tests for configuration validation and loading."""

import pytest

from src.config import (
    AppConfig,
    ConfigurationError,
    CopilotConfig,
    JiraConfig,
    NotificationConfig,
    _load_settings,
)


class TestJiraConfig:
    def test_production_requires_https_base_url(self, monkeypatch):
        monkeypatch.setenv("ENV", "production")
        with pytest.raises(ConfigurationError):
            JiraConfig(base_url="http://jira.example.com", username="u", api_token="t")

    def test_production_requires_username(self, monkeypatch):
        monkeypatch.setenv("ENV", "production")
        with pytest.raises(ConfigurationError):
            JiraConfig(base_url="https://jira.example.com", username="", api_token="t")

    def test_production_requires_api_token(self, monkeypatch):
        monkeypatch.setenv("ENV", "production")
        with pytest.raises(ConfigurationError):
            JiraConfig(base_url="https://jira.example.com", username="u", api_token="")


class TestCopilotConfig:
    def test_temperature_range_validation(self):
        with pytest.raises(ConfigurationError):
            CopilotConfig(api_key="k", model="m", temperature=1.5)

    def test_max_tokens_validation(self):
        with pytest.raises(ConfigurationError):
            CopilotConfig(api_key="k", model="m", max_tokens=64)


class TestNotificationConfig:
    def test_smtp_requires_username(self):
        with pytest.raises(ConfigurationError):
            NotificationConfig(smtp_host="smtp.example.com", smtp_password="pw")

    def test_smtp_requires_password(self):
        with pytest.raises(ConfigurationError):
            NotificationConfig(smtp_host="smtp.example.com", smtp_username="user")

    def test_smtp_port_range_validation(self):
        with pytest.raises(ConfigurationError):
            NotificationConfig(smtp_port=70000)


class TestAppConfig:
    def test_log_level_validation(self):
        with pytest.raises(ConfigurationError):
            AppConfig(log_level="TRACE")

    def test_port_range_validation(self):
        with pytest.raises(ConfigurationError):
            AppConfig(port=0)

    def test_max_comments_validation(self):
        with pytest.raises(ConfigurationError):
            AppConfig(max_comments=0)


class TestLoadSettings:
    def test_load_settings_success(self, monkeypatch):
        monkeypatch.setenv("ENV", "development")
        monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
        monkeypatch.setenv("JIRA_USERNAME", "dev@company.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "token")
        monkeypatch.setenv("COPILOT_TEMPERATURE", "0.2")
        monkeypatch.setenv("COPILOT_MAX_TOKENS", "512")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("APP_PORT", "8000")
        monkeypatch.setenv("MAX_COMMENTS", "20")

        settings = _load_settings()

        assert settings.jira.base_url == "https://jira.example.com"
        assert settings.copilot.temperature == 0.2
        assert settings.copilot.max_tokens == 512
        assert settings.app.max_comments == 20

    def test_load_settings_invalid_number_wrapped(self, monkeypatch):
        monkeypatch.setenv("COPILOT_TEMPERATURE", "not-a-number")

        with pytest.raises(ConfigurationError):
            _load_settings()

    def test_load_settings_config_error_propagates(self, monkeypatch):
        """ConfigurationError from a sub-config propagates unchanged."""
        monkeypatch.setenv("ENV", "development")
        monkeypatch.setenv("COPILOT_TEMPERATURE", "1.5")  # out of range → ConfigurationError

        with pytest.raises(ConfigurationError, match="COPILOT_TEMPERATURE"):
            _load_settings()
