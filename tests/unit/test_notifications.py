"""Tests for notification integrations — Teams + Email.

All network calls are mocked; no real HTTP or SMTP traffic.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.integrations.notifications import (
    TeamsNotifier,
    EmailNotifier,
    NotificationService,
)


# ===================================================================== #
#  TeamsNotifier                                                         #
# ===================================================================== #

class TestTeamsNotifier:
    def test_disabled_when_no_url(self):
        t = TeamsNotifier()
        assert t.enabled is False

    def test_enabled_when_url_provided(self):
        t = TeamsNotifier(webhook_url="https://outlook.office.com/webhook/test")
        assert t.enabled is True

    @patch("src.integrations.notifications.requests.post")
    def test_notify_draft_generated(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        t = TeamsNotifier(webhook_url="https://outlook.office.com/webhook/test")
        result = t.notify_draft_generated(
            draft_id="draft_001",
            issue_key="DEFECT-123",
            classification="cannot_reproduce",
            confidence=0.92,
            body_preview="Thanks for the update...",
        )
        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["@type"] == "MessageCard"
        assert "DEFECT-123" in payload["summary"]

    @patch("src.integrations.notifications.requests.post")
    def test_notify_draft_approved(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        t = TeamsNotifier(webhook_url="https://outlook.office.com/webhook/test")
        result = t.notify_draft_approved(
            draft_id="draft_001",
            issue_key="DEFECT-123",
            approved_by="qa@company.com",
        )
        assert result is True

    @patch("src.integrations.notifications.requests.post")
    def test_notify_draft_rejected(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        t = TeamsNotifier(webhook_url="https://outlook.office.com/webhook/test")
        result = t.notify_draft_rejected(
            draft_id="draft_001",
            issue_key="DEFECT-123",
            feedback="Needs more detail",
        )
        assert result is True

    def test_send_returns_false_when_disabled(self):
        t = TeamsNotifier()
        result = t.notify_draft_generated(
            draft_id="d1", issue_key="X-1",
            classification="other", confidence=0.5, body_preview="hi",
        )
        assert result is False

    @patch("src.integrations.notifications.requests.post", side_effect=Exception("timeout"))
    def test_send_handles_exception(self, mock_post):
        t = TeamsNotifier(webhook_url="https://outlook.office.com/webhook/test")
        result = t.notify_draft_generated(
            draft_id="d1", issue_key="X-1",
            classification="other", confidence=0.5, body_preview="hi",
        )
        assert result is False

    def test_build_card_structure(self):
        card = TeamsNotifier._build_card(
            title="Test Title",
            facts={"Key": "Value"},
            body="Body text",
            color="0078D7",
        )
        assert card["@type"] == "MessageCard"
        assert card["themeColor"] == "0078D7"
        assert len(card["sections"]) == 1
        assert card["sections"][0]["activityTitle"] == "Test Title"
        assert card["sections"][0]["facts"][0]["name"] == "Key"


# ===================================================================== #
#  EmailNotifier                                                         #
# ===================================================================== #

class TestEmailNotifier:
    def test_disabled_when_no_host(self):
        e = EmailNotifier()
        assert e.enabled is False

    def test_disabled_when_no_recipients(self):
        e = EmailNotifier(smtp_host="smtp.example.com", from_address="a@b.com")
        assert e.enabled is False

    def test_enabled_when_fully_configured(self):
        e = EmailNotifier(
            smtp_host="smtp.example.com",
            from_address="bot@company.com",
            to_addresses=["qa@company.com"],
        )
        assert e.enabled is True

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_notify_draft_generated(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        e = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            from_address="bot@company.com",
            to_addresses=["qa@company.com"],
            use_tls=True,
        )
        result = e.notify_draft_generated(
            draft_id="draft_001",
            issue_key="DEFECT-123",
            classification="cannot_reproduce",
            confidence=0.92,
            body_preview="Thanks for the update...",
        )
        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.sendmail.assert_called_once()

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_notify_draft_approved_email(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        e = EmailNotifier(
            smtp_host="smtp.example.com",
            from_address="bot@company.com",
            to_addresses=["qa@company.com"],
        )
        result = e.notify_draft_approved(
            draft_id="draft_001",
            issue_key="DEFECT-123",
            approved_by="qa@company.com",
        )
        assert result is True

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_notify_draft_rejected_email(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        e = EmailNotifier(
            smtp_host="smtp.example.com",
            from_address="bot@company.com",
            to_addresses=["qa@company.com"],
        )
        result = e.notify_draft_rejected(
            draft_id="draft_001",
            issue_key="DEFECT-123",
            feedback="Needs more context",
        )
        assert result is True

    def test_send_returns_false_when_disabled(self):
        e = EmailNotifier()
        result = e.notify_draft_generated(
            draft_id="d1", issue_key="X-1",
            classification="other", confidence=0.5, body_preview="hi",
        )
        assert result is False

    @patch("src.integrations.notifications.smtplib.SMTP", side_effect=Exception("conn refused"))
    def test_send_handles_smtp_exception(self, mock_smtp):
        e = EmailNotifier(
            smtp_host="smtp.example.com",
            from_address="bot@company.com",
            to_addresses=["qa@company.com"],
        )
        result = e.notify_draft_generated(
            draft_id="d1", issue_key="X-1",
            classification="other", confidence=0.5, body_preview="hi",
        )
        assert result is False


# ===================================================================== #
#  NotificationService (facade)                                          #
# ===================================================================== #

class TestNotificationService:
    def test_any_enabled_false_when_both_disabled(self):
        ns = NotificationService()
        assert ns.any_enabled is False

    def test_any_enabled_true_with_teams(self):
        teams = TeamsNotifier(webhook_url="https://test")
        ns = NotificationService(teams=teams)
        assert ns.any_enabled is True

    def test_any_enabled_true_with_email(self):
        email = EmailNotifier(
            smtp_host="smtp.test.com",
            from_address="a@b.com",
            to_addresses=["c@d.com"],
        )
        ns = NotificationService(email=email)
        assert ns.any_enabled is True

    @patch("src.integrations.notifications.requests.post")
    def test_fanout_draft_generated(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        teams = TeamsNotifier(webhook_url="https://test")
        ns = NotificationService(teams=teams)

        results = ns.notify_draft_generated(
            draft_id="d1",
            issue_key="DEFECT-1",
            classification="fix_ready",
            confidence=0.95,
            body_preview="Fix deployed",
        )
        assert results["teams"] is True

    @patch("src.integrations.notifications.requests.post")
    def test_fanout_draft_approved(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        teams = TeamsNotifier(webhook_url="https://test")
        ns = NotificationService(teams=teams)

        results = ns.notify_draft_approved(
            draft_id="d1", issue_key="DEFECT-1", approved_by="qa",
        )
        assert results["teams"] is True

    @patch("src.integrations.notifications.requests.post")
    def test_fanout_draft_rejected(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        teams = TeamsNotifier(webhook_url="https://test")
        ns = NotificationService(teams=teams)

        results = ns.notify_draft_rejected(
            draft_id="d1", issue_key="DEFECT-1", feedback="nope",
        )
        assert results["teams"] is True

    def test_fanout_skips_disabled_channels(self):
        ns = NotificationService()
        results = ns.notify_draft_generated(
            draft_id="d1", issue_key="X-1",
            classification="other", confidence=0.5, body_preview="hi",
        )
        assert results == {}

    @patch("src.integrations.notifications.requests.post")
    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_fanout_both_channels(self, mock_smtp_class, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        teams = TeamsNotifier(webhook_url="https://test")
        email = EmailNotifier(
            smtp_host="smtp.test.com",
            from_address="a@b.com",
            to_addresses=["c@d.com"],
        )
        ns = NotificationService(teams=teams, email=email)

        results = ns.notify_draft_generated(
            draft_id="d1", issue_key="DEFECT-1",
            classification="blocked", confidence=0.88, body_preview="Blocked",
        )
        assert results["teams"] is True
        assert results["email"] is True
