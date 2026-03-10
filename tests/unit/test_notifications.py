"""Tests for Teams and Email notification senders (Phase 5)."""

from unittest.mock import MagicMock, patch

from src.integrations.notifications import (
    EmailNotifier,
    TeamsNotifier,
    notify_draft_event,
    notify_draft_ready,
)

# Shared sample draft
SAMPLE_DRAFT = {
    "draft_id": "draft_abc123",
    "issue_key": "DEFECT-404",
    "body": "Thanks for the update. We are investigating the issue.",
    "classification": "cannot_reproduce",
    "comment_type": "cannot_reproduce",
    "confidence_score": 0.9,
    "status": "generated",
}

# TeamsNotifier
class TestTeamsNotifier:

    def test_not_configured_returns_false(self):
        """No webhook URL → is_configured is False and send returns False."""
        notifier = TeamsNotifier(webhook_url="")
        assert notifier.is_configured is False
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.requests.post")
    def test_send_success_200(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        assert notifier.send(SAMPLE_DRAFT) is True
        mock_post.assert_called_once()

    @patch("src.integrations.notifications.requests.post")
    def test_send_success_202(self, mock_post):
        """202 Accepted is also a success."""
        mock_post.return_value = MagicMock(status_code=202)
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        assert notifier.send(SAMPLE_DRAFT) is True

    @patch("src.integrations.notifications.requests.post")
    def test_send_non_200_returns_false(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="Server Error")
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.requests.post")
    def test_send_400_returns_false(self, mock_post):
        mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.requests.post")
    def test_network_error_returns_false(self, mock_post):
        mock_post.side_effect = ConnectionError("unreachable")
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.requests.post")
    def test_timeout_error_returns_false(self, mock_post):
        mock_post.side_effect = Exception("timeout")
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.requests.post")
    def test_long_body_truncated_in_card(self, mock_post):
        """Body longer than 300 chars is truncated with '…' in the card."""
        mock_post.return_value = MagicMock(status_code=200)
        long_draft = {**SAMPLE_DRAFT, "body": "x" * 500}
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.send(long_draft)

        card = mock_post.call_args[1]["json"]
        body_blocks = card["attachments"][0]["content"]["body"]
        preview_block = body_blocks[-1]
        assert preview_block["text"].endswith("…")
        assert len(preview_block["text"]) <= 304  # 300 chars + ellipsis

    @patch("src.integrations.notifications.requests.post")
    def test_short_body_not_truncated(self, mock_post):
        """Body at 300 chars or less is not truncated."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.send(SAMPLE_DRAFT)

        card = mock_post.call_args[1]["json"]
        body_blocks = card["attachments"][0]["content"]["body"]
        preview = body_blocks[-1]["text"]
        assert not preview.endswith("…")

    @patch("src.integrations.notifications.requests.post")
    def test_card_contains_issue_key_and_classification(self, mock_post):
        """Adaptive Card FactSet should include issue key and classification."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.send(SAMPLE_DRAFT)

        card = mock_post.call_args[1]["json"]
        fact_set = card["attachments"][0]["content"]["body"][1]
        facts = {f["title"]: f["value"] for f in fact_set["facts"]}
        assert facts["Issue"] == "DEFECT-404"
        assert facts["Classification"] == "cannot_reproduce"
        assert facts["Draft ID"] == "draft_abc123"

    @patch("src.integrations.notifications.requests.post")
    def test_send_with_minimal_draft_does_not_crash(self, mock_post):
        """Draft missing optional keys must not raise."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        result = notifier.send({"draft_id": "d1"})
        assert result is True

    @patch("src.integrations.notifications.requests.post")
    def test_uses_comment_type_fallback_for_classification(self, mock_post):
        """Falls back to 'comment_type' key when 'classification' is absent."""
        mock_post.return_value = MagicMock(status_code=200)
        draft_no_classification = {
            **SAMPLE_DRAFT,
            "comment_type": "by_design",
        }
        del draft_no_classification["classification"]
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.send(draft_no_classification)

        card = mock_post.call_args[1]["json"]
        fact_set = card["attachments"][0]["content"]["body"][1]
        facts = {f["title"]: f["value"] for f in fact_set["facts"]}
        assert facts["Classification"] == "by_design"

    @patch("src.integrations.notifications.requests.post")
    def test_content_type_header_is_json(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.send(SAMPLE_DRAFT)
        headers = mock_post.call_args[1]["headers"]
        assert headers["Content-Type"] == "application/json"

    def test_reads_webhook_url_from_env(self, monkeypatch):
        """Constructor reads TEAMS_WEBHOOK_URL from environment."""
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://env-teams-hook.example.com")
        notifier = TeamsNotifier()
        assert notifier.webhook_url == "https://env-teams-hook.example.com"
        assert notifier.is_configured is True
        
# EmailNotifier
class TestEmailNotifier:

    def test_not_configured_returns_false(self):
        """No SMTP settings → is_configured is False, send returns False."""
        notifier = EmailNotifier()
        assert notifier.is_configured is False
        assert notifier.send(SAMPLE_DRAFT) is False

    def test_missing_from_address_not_configured(self):
        """email_from is required for is_configured."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="u",
            smtp_password="p",
            email_from="",
        )
        assert notifier.is_configured is False

    def test_configured_but_no_recipients_returns_false(self):
        """is_configured True but empty email_to returns False from send."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="",
        )
        assert notifier.is_configured is True
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_send_success(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="qa@example.com",
        )
        result = notifier.send(SAMPLE_DRAFT)

        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user@example.com", "secret")
        mock_server.sendmail.assert_called_once()

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_send_to_multiple_recipients(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="qa@example.com, lead@example.com",
        )
        result = notifier.send(SAMPLE_DRAFT)

        assert result is True
        _, toaddrs, _ = mock_server.sendmail.call_args[0]
        assert toaddrs == ["qa@example.com", "lead@example.com"]

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_smtp_connection_error_returns_false(self, mock_smtp_cls):
        mock_smtp_cls.side_effect = ConnectionRefusedError("connection refused")

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="qa@example.com",
        )
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_smtp_auth_error_returns_false(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_server.login.side_effect = Exception("Authentication failed")

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="user@example.com",
            smtp_password="wrong",
            email_from="from@example.com",
            email_to="qa@example.com",
        )
        assert notifier.send(SAMPLE_DRAFT) is False

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_email_subject_contains_issue_key_and_classification(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="qa@example.com",
        )
        notifier.send(SAMPLE_DRAFT)

        _, _, raw_msg = mock_server.sendmail.call_args[0]
        assert "DEFECT-404" in raw_msg
        assert "cannot_reproduce" in raw_msg

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_email_body_contains_draft_content(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="qa@example.com",
        )
        notifier.send(SAMPLE_DRAFT)

        _, _, raw_msg = mock_server.sendmail.call_args[0]
        assert SAMPLE_DRAFT["body"] in raw_msg
        assert SAMPLE_DRAFT["draft_id"] in raw_msg

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_smtp_uses_correct_host_and_port(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="mail.company.com",
            smtp_port=465,
            smtp_username="user@example.com",
            smtp_password="secret",
            email_from="from@example.com",
            email_to="qa@example.com",
        )
        notifier.send(SAMPLE_DRAFT)

        mock_smtp_cls.assert_called_once_with("mail.company.com", 465, timeout=15)

    def test_reads_config_from_env(self, monkeypatch):
        """Constructor reads SMTP_* and EMAIL_* from environment."""
        monkeypatch.setenv("SMTP_HOST", "smtp.env.com")
        monkeypatch.setenv("SMTP_PORT", "465")
        monkeypatch.setenv("SMTP_USERNAME", "env_user@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "env_pass")
        monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
        monkeypatch.setenv("EMAIL_TO", "team@example.com")
        notifier = EmailNotifier()
        assert notifier.smtp_host == "smtp.env.com"
        assert notifier.smtp_port == 465
        assert notifier.email_from == "noreply@example.com"
        assert notifier.is_configured is True


# ---------------------------------------------------------------------------
# notify_draft_ready (orchestrator)
# ---------------------------------------------------------------------------


class TestNotifyDraftReady:

    def test_no_channels_configured_returns_empty_dict(self, monkeypatch):
        """When neither Teams nor Email is configured, result is {}."""
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "")
        monkeypatch.setenv("SMTP_HOST", "")
        result = notify_draft_ready(SAMPLE_DRAFT)
        assert result == {}

    @patch("src.integrations.notifications.requests.post")
    def test_teams_channel_called_and_succeeds(self, mock_post, monkeypatch):
        mock_post.return_value = MagicMock(status_code=200)
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://teams.example.com/hook")
        monkeypatch.setenv("SMTP_HOST", "")
        result = notify_draft_ready(SAMPLE_DRAFT)
        assert "teams" in result
        assert result["teams"] is True

    @patch("src.integrations.notifications.requests.post")
    def test_teams_channel_failure_recorded(self, mock_post, monkeypatch):
        mock_post.return_value = MagicMock(status_code=500, text="Error")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://teams.example.com/hook")
        monkeypatch.setenv("SMTP_HOST", "")
        result = notify_draft_ready(SAMPLE_DRAFT)
        assert result["teams"] is False

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_email_channel_called_and_succeeds(self, mock_smtp_cls, monkeypatch):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_USERNAME", "user@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("EMAIL_FROM", "from@example.com")
        monkeypatch.setenv("EMAIL_TO", "qa@example.com")

        result = notify_draft_ready(SAMPLE_DRAFT)
        assert "email" in result
        assert result["email"] is True

    @patch("src.integrations.notifications.smtplib.SMTP")
    @patch("src.integrations.notifications.requests.post")
    def test_both_channels_when_configured(self, mock_post, mock_smtp_cls, monkeypatch):
        mock_post.return_value = MagicMock(status_code=200)
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://teams.example.com/hook")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_USERNAME", "user@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("EMAIL_FROM", "from@example.com")
        monkeypatch.setenv("EMAIL_TO", "qa@example.com")

        result = notify_draft_ready(SAMPLE_DRAFT)
        assert "teams" in result
        assert "email" in result


class TestNotifyDraftEvent:

    @patch("src.integrations.notifications.requests.post")
    def test_teams_approved_event_card_contains_event(self, mock_post, monkeypatch):
        mock_post.return_value = MagicMock(status_code=200)
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://teams.example.com/hook")
        monkeypatch.setenv("SMTP_HOST", "")

        result = notify_draft_event(
            SAMPLE_DRAFT,
            event_name="approved",
            actor="qa@company.com",
        )

        assert result["teams"] is True
        card = mock_post.call_args[1]["json"]
        title = card["attachments"][0]["content"]["body"][0]["text"]
        facts = card["attachments"][0]["content"]["body"][1]["facts"]
        fact_map = {f["title"]: f["value"] for f in facts}
        assert "Approved" in title
        assert fact_map["Event"] == "approved"

    @patch("src.integrations.notifications.smtplib.SMTP")
    def test_email_rejected_event_subject_contains_rejected(self, mock_smtp_cls, monkeypatch):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        monkeypatch.setenv("TEAMS_WEBHOOK_URL", "")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_USERNAME", "user@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("EMAIL_FROM", "from@example.com")
        monkeypatch.setenv("EMAIL_TO", "qa@example.com")

        result = notify_draft_event(
            SAMPLE_DRAFT,
            event_name="rejected",
            feedback="Needs better logs",
        )

        assert result["email"] is True
        _, _, raw_msg = mock_server.sendmail.call_args[0]
        assert "rejected" in raw_msg.lower()
