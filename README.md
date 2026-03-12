# AI Assistant for Jira Comment Replies

**Intern Project 2026** — Nagasai & Yousef

AI assistant for Jira defect comments. It listens to Jira webhook events, classifies comment intent, collects issue context, generates a draft reply, and supports manual approve/reject with optional Jira posting and notifications.

---

## Completed

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

---

### MVP v2 — Phase 1: Config, SQLite & Classification Enhancements
- [x] Centralised config — `AppConfig` with nested Jira, Copilot, LLM, RAG, Confluence, TestRail, Log, Notification configs
- [x] Local LLM support — llama.cpp / GGUF backend via `LLM_BACKEND=local`
- [x] SQLite draft store — persistent storage with WAL mode, indexed queries
- [x] 8 classification buckets — cannot_reproduce, need_more_info, fixed_validate, by_design, duplicate_fixed, blocked_waiting, config_issue, other
- [x] Drafter templates — one template per bucket with evidence, citations, suggested labels & actions
- [x] RAG, Confluence, TestRail, Log Lookup data models — ready for Phase 2+
- [x] **Final:** 138 unit + integration tests passing

### MVP v2 — Phase 2: RAG Engine & Document Ingestion
- [x] RAG engine — ChromaDB-backed vector store with sentence-transformer embeddings
- [x] Document ingester — sliding-window chunking with paragraph/sentence boundary preference
- [x] PDF ingestion — parse via pypdf, chunk, and index
- [x] Confluence client — fetch pages, search by space/label, HTML-to-text conversion
- [x] Confluence ingestion — discover and index pages into RAG
- [x] RAG API endpoints — ingest (PDF, text, Confluence), search, stats, delete
- [x] Context collector integration — queries RAG for relevant snippets during context gathering
- [x] Drafter integration — RAG snippets in evidence formatting, citations, and evidence tracking
- [x] **Final:** 211 unit + integration tests, 82% code coverage

## Architecture

```
Jira Cloud (Webhook)
    │
    ▼
POST /webhook/jira  ──▶  EventFilter
    │
    ▼
CommentClassifier  ──▶  Keyword rules │ Copilot SDK fallback  (8 buckets)
    │
    ▼
ContextCollector   ──▶  Issue fields, comments, attachments, changelog, Jenkins links
    │                    + RAG engine semantic search (when configured)
    ▼
ResponseDrafter    ──▶  Template fill + RAG evidence + optional Copilot polish
    │
    ▼
SQLite Store       ──▶  GET /drafts  │  POST /approve  │  POST /reject
    │
    ▼
Notifications      ──▶  Teams Webhook (card)  │  Email (SMTP)

    ┌───────────────────────────────────────────────────────┐
    │  RAG Pipeline (Phase 2)                               │
    │  POST /rag/ingest/pdf  ──▶  pypdf → chunker → ChromaDB│
    │  POST /rag/ingest/text ──▶  chunker → ChromaDB        │
    │  POST /rag/ingest/confluence ──▶ Confluence → chunker  │
    │  GET  /rag/search      ──▶  semantic query → snippets  │
    └───────────────────────────────────────────────────────┘
```

## Classification Buckets

| Bucket | Typical triggers |
|---|---|
| Cannot Repro | "cannot reproduce", "can't repro", "works on my machine" |
| Need Info / Logs | "need logs", "provide logs", "need more info" |
| Fixed — Validate | "fix ready", "fix deployed", "please validate", "already fixed" |
| By Design | "as designed", "by design", "expected behavior" |
| Other | (fallback) |

## Project Structure

```
├── src/
│   ├── config.py                 # Centralised settings (env vars)
│   ├── agent/
│   │   ├── classifier.py         # Comment classification (keywords + Copilot SDK)
│   │   ├── context_collector.py  # Jira context + RAG snippet gathering
│   │   └── drafter.py            # Template-based draft generation with RAG evidence
│   ├── api/
│   │   ├── app.py                # FastAPI webhook, approval & RAG endpoints
│   │   └── event_filter.py       # Webhook event gate rules
│   ├── integrations/
│   │   ├── confluence.py         # Confluence Cloud API client (RAG ingestion)
│   │   ├── jira.py               # Jira Cloud REST API client
│   │   └── notifications.py      # Teams webhook + Email (SMTP) notifier
│   ├── models/
│   │   ├── classification.py     # CommentType enum + classification model
│   │   ├── comment.py            # Comment data model
│   │   ├── context.py            # IssueContext + collection result
│   │   ├── draft.py              # Draft + DraftStatus models
│   │   ├── rag.py                # DocumentChunk, RAGSnippet, RAGResult models
│   │   └── webhook.py            # JiraWebhookEvent payload model
│   ├── rag/
│   │   ├── engine.py             # ChromaDB-backed semantic retrieval engine
│   │   └── ingest.py             # Document ingestion pipeline (PDF, text, Confluence)
│   └── storage/
│       └── sqlite_store.py       # SQLite persistent draft store (WAL mode)
├── tests/
│   ├── conftest.py               # Shared fixtures
│   └── unit/
│       ├── test_classifier.py
│       ├── test_confluence.py    # Confluence client tests
│       ├── test_context_collector.py
│       ├── test_drafter.py
│       ├── test_ingester.py      # Document ingester tests
│       ├── test_jira_client.py
│       ├── test_notifications.py
│       ├── test_pipeline.py      # End-to-end via FastAPI TestClient
│       ├── test_rag_engine.py    # RAG engine tests
│       ├── test_rag_integration.py # RAG + drafter/collector integration
│       └── test_webhook_filter.py
├── docs/
│   ├── ARCHITECTURE.md
│   └── SETUP.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── pytest.ini
└── README.md
```

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
