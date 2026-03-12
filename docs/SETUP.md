# Setup Guide

## Prerequisites

- Python 3.10+
- Jira Cloud instance with API access
- Copilot SDK API key for AI-powered classification & refinement (optional)

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

#### Core Settings

| Variable | Required | Description |
|---|---|---|
| `JIRA_BASE_URL` | Yes (for live Jira) | e.g. `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Yes (for live Jira) | Your Jira email |
| `JIRA_API_TOKEN` | Yes (for live Jira) | Generate in Jira → Personal Settings → API tokens |
| `COPILOT_API_KEY` | No | Leave blank for keyword-only mode |
| `COPILOT_MODEL` | No | Default: `gpt-4` |
| `APP_PORT` | No | Default: `8000` |
| `ASSISTANT_DB_PATH` | No | Default: `.data/assistant.db` |

#### Local LLM Settings

| Variable | Required | Description |
|---|---|---|
| `LLM_BACKEND` | No | `copilot` (default) or `local` for llama.cpp |
| `LLM_MODEL_PATH` | If `local` | Path to `.gguf` model file |
| `LLM_N_CTX` | No | Context window size (default: `4096`) |
| `LLM_N_GPU_LAYERS` | No | GPU layers (default: `0` = CPU only) |
| `LLM_TEMPERATURE` | No | Default: `0.1` |
| `LLM_MAX_TOKENS` | No | Default: `1024` |
| `LLM_N_THREADS` | No | Default: `4` |

#### RAG Settings

| Variable | Required | Description |
|---|---|---|
| `CHROMA_PERSIST_DIR` | No | Default: `.data/chroma` |
| `RAG_EMBEDDING_MODEL` | No | Default: `all-MiniLM-L6-v2` |
| `RAG_CHUNK_SIZE` | No | Default: `500` chars |
| `RAG_CHUNK_OVERLAP` | No | Default: `50` chars |
| `RAG_TOP_K` | No | Default: `5` snippets |
| `PDF_UPLOAD_DIR` | No | Default: `.data/pdfs` |

#### Confluence Settings

| Variable | Required | Description |
|---|---|---|
| `CONFLUENCE_BASE_URL` | No | Confluence Cloud URL |
| `CONFLUENCE_USERNAME` | No | Confluence email |
| `CONFLUENCE_API_TOKEN` | No | Confluence API token |
| `CONFLUENCE_SPACES` | No | Comma-separated space keys to index |
| `CONFLUENCE_LABELS` | No | Comma-separated labels to filter pages |

#### TestRail Settings

| Variable | Required | Description |
|---|---|---|
| `TESTRAIL_BASE_URL` | No | TestRail instance URL |
| `TESTRAIL_USERNAME` | No | TestRail email |
| `TESTRAIL_API_KEY` | No | TestRail API key |

#### Log Lookup Settings

| Variable | Required | Description |
|---|---|---|
| `JENKINS_BASE_URL` | No | Jenkins server URL |
| `JENKINS_USERNAME` | No | Jenkins username |
| `JENKINS_API_TOKEN` | No | Jenkins API token |
| `LOG_DIR` | No | Local log directory path |
| `LOG_TIME_WINDOW_HOURS` | No | Default: `24` |

#### Notification Settings

| Variable | Required | Description |
|---|---|---|
| `TEAMS_WEBHOOK_URL` | No | Teams incoming webhook URL for notifications |
| `SMTP_HOST` | No | SMTP server hostname (leave blank to disable email) |
| `SMTP_PORT` | No | Default: `587` |
| `SMTP_USERNAME` | No | SMTP login username |
| `SMTP_PASSWORD` | No | SMTP login password |
| `EMAIL_FROM` | No | Sender email address |
| `EMAIL_TO` | No | Comma-separated recipient addresses |

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

# Ingest a text document into RAG
curl -X POST http://localhost:8000/rag/ingest/text \
  -H "Content-Type: application/json" \
  -d '{"title": "Auth Runbook", "text": "Check SSO config when login fails...", "source_type": "runbook"}'

# Search the RAG index
curl "http://localhost:8000/rag/search?q=login+failure&top_k=3"

# RAG collection stats
curl http://localhost:8000/rag/stats
```

## Optional: RAG Dependencies

The RAG engine and document ingestion pipeline require additional packages
that are **not** installed by default (they are only needed if you use the
`/rag/*` endpoints):

```bash
pip install chromadb sentence-transformers pypdf
```

- **chromadb** — vector store for semantic retrieval
- **sentence-transformers** — embedding model (`all-MiniLM-L6-v2`)
- **pypdf** — PDF text extraction

The core pipeline (webhook → classify → context → draft) works without
these packages.  Tests mock all heavy dependencies so `pytest` runs
without installing them.

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
