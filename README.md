# AI Assistant for Jira Comment Replies

**Intern Project 2026** — Nagasai & Yousef

AI assistant for Jira defect comments. It listens to Jira webhook events, classifies comment intent, collects issue context, generates a draft reply, and supports manual approve/reject with optional Jira posting and notifications.

---

## MVP v1 Status

### Phase 1: Architecture & Scaffolding
- [x] Project structure, config, and `.env` template
- [x] Core models (`Comment`, `Classification`, `Context`, `Draft`, `Webhook`)
- [x] Jira REST integration (`JiraClient`)
- [x] Webhook receiver (`POST /webhook/jira`)
- [x] Event filtering (issue type, status, keywords, idempotency)
- [x] Unit tests for Jira client and webhook filter

### Phase 2: Comment Classification
- [x] 4-bucket keyword classification with optional Copilot SDK fallback
- [x] Unit tests for classifier

### Phase 3: Context Collection & Draft Generation
- [x] Context collection (issue fields, recent comments, attachments, linked issues, changelog)
- [x] Jenkins log URL extraction
- [x] Template-based drafting with optional LLM polish
- [x] Evidence/citation tracking + suggested labels/actions
- [x] Unit tests for context collector and drafter

### Phase 4: Full Pipeline & Approval Workflow
- [x] End-to-end pipeline orchestration
- [x] Draft storage + API (`GET /drafts`, `GET /drafts/{id}`)
- [x] Approve/reject workflow (`POST /approve`, `POST /reject`)
- [x] Integration tests via FastAPI `TestClient`

### Phase 5: Notifications
- [x] Optional Teams webhook and Email (SMTP)
- [x] Notification tests

## High-Level Flow

```
Jira Cloud (Webhook)
    │
    ▼
POST /webhook/jira  ──▶  EventFilter
    │
    ▼
CommentClassifier
    │
    ▼
ContextCollector
    │
    ▼
ResponseDrafter
    │
    ▼
SQLite Draft Store  ──▶  GET /drafts | POST /approve | POST /reject
    │
    ▼
Optional Notifications (Teams / Email)
```

## Classification Buckets

| Bucket | Typical triggers |
|---|---|
| Cannot Repro | "cannot reproduce", "can't repro", "works on my machine" |
| Need Info / Logs | "need logs", "provide logs", "need more info" |
| Fixed — Validate | "fix ready", "fix deployed", "please validate", "already fixed" |
| By Design | "as designed", "by design", "expected behavior" |
| Other | fallback |

## Quick Start

```bash
# 1) Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2) Install project + dev dependencies
pip install -e ".[dev]"

# 3) Configure environment
cp .env.example .env

# 4) Run tests
pytest -q

# 5) Run API
uvicorn src.api.app:app --host 127.0.0.1 --port 8000

# 6) Verify service
curl -sS http://127.0.0.1:8000/health
```

## Required Configuration

### Minimum for local development
- `JIRA_BASE_URL`
- `JIRA_USERNAME`
- `JIRA_API_TOKEN`

### Required for secured production use
- `WEBHOOK_SECRET` (HMAC signature validation)
- `APPROVAL_API_KEY` (required on approve/reject endpoints)
- `ASSISTANT_DB_PATH` (persistent SQLite DB path)

### Optional features
- `COPILOT_API_KEY` (LLM enhancement)
- `TEAMS_WEBHOOK_URL` (Teams notifications)
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` (email notifications)

## API Endpoints

- `GET /health` — health check
- `POST /webhook/jira` — receive Jira comment webhook events
- `GET /drafts` — list drafts (supports `issue_key` filter)
- `GET /drafts/{draft_id}` — get one draft
- `POST /approve` — approve draft (and attempt Jira post)
- `POST /reject` — reject draft with feedback

### Auth headers
- Webhook signature: `X-Hub-Signature-256: sha256=<digest>` (or `X-Webhook-Signature: <digest>`)
- Approve/reject token: `X-Approval-Token: <APPROVAL_API_KEY>`

## Project Structure

```
├── src/
│   ├── api/                  # FastAPI endpoints and event filtering
│   ├── agent/                # Classification, context collection, drafting
│   ├── integrations/         # Jira + notification adapters
│   ├── models/               # Pydantic data models
│   ├── storage/              # SQLite persistence
│   └── config.py             # Environment-driven settings
├── tests/                    # Unit/integration tests
├── docs/
│   ├── ARCHITECTURE.md
│   └── SETUP.md
├── .env.example
├── pyproject.toml
└── README.md
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Setup Guide](docs/SETUP.md)
