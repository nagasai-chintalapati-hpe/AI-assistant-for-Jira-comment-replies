# Setup Guide

## Prerequisites

- Python 3.10+
- Jira Cloud project and API token
- Optional: Copilot API key for LLM enhancement

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure Environment

```bash
cp .env.example .env
```

Set these values in `.env`.

### Required for live Jira use

| Variable | Description |
|---|---|
| `JIRA_BASE_URL` | Jira base URL, e.g. `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Jira user email |
| `JIRA_API_TOKEN` | Jira API token |

### Required for secured production

| Variable | Description |
|---|---|
| `WEBHOOK_SECRET` | HMAC secret for webhook signature verification |
| `APPROVAL_API_KEY` | Token required by `/approve` and `/reject` |
| `ASSISTANT_DB_PATH` | SQLite file path for persistence/idempotency |

### Optional

| Variable | Description |
|---|---|
| `COPILOT_API_KEY` | Enables LLM fallback/enhancement |
| `COPILOT_MODEL` | Default: `claude-sonnet-4.5` |
| `LLM_PROVIDER` | `copilot`, `llama_cpp`, `local`, or `openai_compat` |
| `TEAMS_WEBHOOK_URL` | Teams notifications |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` | Email notifications |

## Run API

```bash
uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl -sS http://127.0.0.1:8000/health
```

## Run Tests

```bash
pytest -q
```

Coverage report:

```bash
pytest --cov=src --cov-report=term-missing --cov-report=html
open htmlcov/index.html
```

## API Endpoints

- `GET /health`
- `POST /webhook/jira`
- `GET /drafts`
- `GET /drafts/{draft_id}`
- `POST /approve`
- `POST /reject`

## Security Headers

When `WEBHOOK_SECRET` is configured, send one of:

- `X-Hub-Signature-256: sha256=<hex-digest>`
- `X-Webhook-Signature: <hex-digest>`

When `APPROVAL_API_KEY` is configured, include:

- `X-Approval-Token: <APPROVAL_API_KEY>`

## Local Webhook Test

```bash
payload='{"webhookEvent":"comment_created","timestamp":1700000001,"issue":{"id":"1","key":"DEFECT-500","fields":{"summary":"Upload crash","issuetype":{"name":"Bug"},"status":{"name":"Open"}}},"comment":{"id":"90001","body":"Cannot reproduce this on my machine.","author":{"accountId":"u1","displayName":"Dev","emailAddress":"dev@company.com"},"created":"2025-02-23T10:30:00.000+0000","updated":"2025-02-23T10:30:00.000+0000"}}'
sig=$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')

curl -sS -X POST http://127.0.0.1:8000/webhook/jira \
  -H 'Content-Type: application/json' \
  -H "X-Hub-Signature-256: sha256=$sig" \
  -d "$payload"
```

Fetch drafts for an issue:

```bash
curl -sS 'http://127.0.0.1:8000/drafts?issue_key=DEFECT-500'
```

Approve a draft:

```bash
curl -sS -X POST http://127.0.0.1:8000/approve \
  -H 'Content-Type: application/json' \
  -H "X-Approval-Token: $APPROVAL_API_KEY" \
  -d '{"draft_id":"<DRAFT_ID>","approved_by":"qa@company.com"}'
```

## Jira Webhook Setup

1. Jira Settings → System → Webhooks
2. Create webhook URL: `https://<host>/webhook/jira`
3. Select comment events (`comment_created`, `comment_updated`)
4. Save

For local testing, expose port 8000 with ngrok and use the ngrok HTTPS URL.

## Troubleshooting

### Missing Jira config

Set `JIRA_BASE_URL`, `JIRA_USERNAME`, and `JIRA_API_TOKEN` in `.env`.

### Missing/invalid webhook signature

Ensure `WEBHOOK_SECRET` matches, and sign the raw payload with HMAC-SHA256.

### Missing/invalid approval token

Set `APPROVAL_API_KEY`, and send `X-Approval-Token` for approve/reject calls.

### Notifications not sending

- Teams: verify `TEAMS_WEBHOOK_URL`
- Email: verify SMTP variables and sender/recipient fields
