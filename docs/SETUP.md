# Setup Guide — MVP v1

## Prerequisites

- Python 3.10+
- Jira Cloud instance with API access
- (Optional) Copilot SDK API key for AI-powered classification & refinement

## Installation

### 1. Clone & enter the project

```bash
cd AI-assistant-for-Jira-comment-replies
```

### 2. Create and activate virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -e .           # Production deps
pip install -e ".[dev]"    # + dev tools (black, ruff, mypy, pytest-asyncio)
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Required | Description |
|---|---|---|
| `JIRA_BASE_URL` | Yes (for live Jira) | e.g. `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Yes (for live Jira) | Your Jira email |
| `JIRA_API_TOKEN` | Yes (for live Jira) | Generate in Jira → Personal Settings → API tokens |
| `COPILOT_API_KEY` | No | Leave blank for keyword-only mode |
| `COPILOT_MODEL` | No | Default: `gpt-4` |
| `APP_PORT` | No | Default: `8000` |
| `TEAMS_WEBHOOK_URL` | No | Teams incoming webhook URL for notifications |
| `SMTP_HOST` | No | SMTP server hostname (leave blank to disable email) |
| `SMTP_PORT` | No | Default: `587` |
| `SMTP_USERNAME` | No | SMTP login username |
| `SMTP_PASSWORD` | No | SMTP login password |
| `EMAIL_FROM` | No | Sender email address |
| `EMAIL_TO` | No | Comma-separated recipient addresses |

## Running the Application

### Development

```bash
uvicorn src.api.app:app --reload --host 0.0.0.0 --port 8000
```

### Verify it's running

```bash
curl http://localhost:8000/health
```

## Running Tests

```bash
# All tests with coverage
pytest

# Verbose output
pytest -v --tb=short

# Specific test file
pytest tests/unit/test_classifier.py

# Coverage report in browser
pytest --cov=src --cov-report=html
open htmlcov/index.html
```

## Registering a Jira Webhook

1. Go to **Jira Settings → System → Webhooks**
2. Click **Create a webhook**
3. Set URL: `https://<your-host>:8000/webhook/jira`
4. Select events: `comment_created`, `comment_updated`
5. Save

> **Tip:** For local development, use [ngrok](https://ngrok.com/) to expose your local server:
> ```bash
> ngrok http 8000
> ```
> Then use the ngrok URL in the Jira webhook configuration.

## Testing with curl

```bash
# Simulate a "cannot reproduce" comment
curl -X POST http://localhost:8000/webhook/jira \
  -H "Content-Type: application/json" \
  -d '{
    "webhookEvent": "comment_created",
    "timestamp": 1700000001,
    "issue": {
      "id": "1", "key": "DEFECT-500",
      "fields": {
        "summary": "Upload crash",
        "issuetype": {"name": "Bug"},
        "status": {"name": "Open"}
      }
    },
    "comment": {
      "id": "90001",
      "body": "Cannot reproduce this on my machine.",
      "author": {"accountId": "u1", "displayName": "Dev", "emailAddress": "dev@co.com"},
      "created": "2025-02-23T10:30:00.000+0000",
      "updated": "2025-02-23T10:30:00.000+0000"
    }
  }'

# List drafts
curl http://localhost:8000/drafts

# Approve a draft
curl -X POST http://localhost:8000/approve \
  -H "Content-Type: application/json" \
  -d '{"draft_id": "<DRAFT_ID>", "approved_by": "qa@company.com"}'
```

## Troubleshooting

### "Missing Jira configuration in environment variables"
- Ensure `JIRA_BASE_URL`, `JIRA_USERNAME`, and `JIRA_API_TOKEN` are set in `.env`
- This error only occurs when context collection tries to call live Jira — tests mock the client

### Tests fail with import errors
- Make sure you installed with `pip install -e .`
- Verify your venv is activated: `which python` should point to `.venv/bin/python`

### "Copilot SDK classification failed"
- Check `COPILOT_API_KEY` is valid
- The system gracefully falls back to keyword classification — no action required

### Notifications not sending
- **Teams:** verify `TEAMS_WEBHOOK_URL` is a valid incoming-webhook URL
- **Email:** ensure `SMTP_HOST`, `EMAIL_FROM`, and `EMAIL_TO` are all set
- Both channels are optional — the system works without them
