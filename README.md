# AI Assistant for Jira Comment Replies

**Intern Project 2026** — Nagasai & Yousef

An AI assistant that generates context-aware draft replies to developer comments on Jira defects, using keyword heuristics with optional Copilot SDK.

---

## Completed

### Phase 1: Architecture & Scaffolding
- [x] Project structure, config, `.env` template
- [x] Main models — Comment, Classification, Context, Draft, Webhook
- [x] Jira integration — full REST API client (`JiraClient`)
- [x] Webhook receiver — `POST /webhook/jira` accepts Jira comment events
- [x] Event filtering — gates on issue type (Bug/Defect), status, trigger keywords, idempotency
- [x] Tests — Jira client & webhook filter

### Phase 2: Comment Classification
- [x] Comment classification — 4 buckets via keywords and Copilot SDK
- [x] Tests — classifier unit tests

### Phase 3: Context Collection & Draft Generation
- [x] Context retrieval — Jira issue fields, last N comments, attachments, linked issues, changelog
- [x] Jenkins link detection — extracts console-log URLs from descriptions & comments
- [x] Draft generation — template-per-bucket with context and Copilot SDK cleanup
- [x] Evidence & citations — attachments and Jenkins logs tracked per draft
- [x] Suggested labels and actions — auto-suggested per classification type
- [x] Tests — context collector & drafter unit tests

### Phase 4: Full Pipeline & Approval Workflow
- [x] Full pipeline orchestration — webhook → filter → classify → context → draft → store
- [x] Draft store & API — `GET /drafts`, `GET /drafts/{id}`, filter by issue key
- [x] Approval workflow — `POST /approve`, `POST /reject` with feedback
- [x] Tests — end-to-end pipeline tests via FastAPI TestClient

### Phase 5: Notifications
- [x] Notifications — Teams webhook cards + Email SMTP on draft generated, approved or rejected
- [x] NotificationService facade — fan-out to channels
- [x] Wired into pipeline, approve, and reject endpoints
- [x] Tests — notification unit tests Teams, Email, service facade
- [x] **Final:** 89 unit + integration tests, 78% code coverage 

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

### MVP v2 — Phase 3: TestRail & Jenkins Log Integration
- [x] `TestRailClient` — API key + session-cookie auth, get_run, get_run_summary, failed tests
- [x] `LogLookupService` — Jenkins console fetch, local file grep, build metadata extraction
- [x] Context collector — detects TestRail run IDs from issue text, fetches summaries; fetches Jenkins console logs for detected URLs
- [x] Drafter — TestRail pass-rate and failed tests in evidence, retest checklist, build metadata in citations
- [x] Tests — `test_testrail.py`, `test_log_lookup.py`, updated context/drafter tests
- [x] **Final:** 331 unit + integration tests passing

### MVP v2 — Phase 4: Git PR Metadata + ELK/OpenSearch Logs
- [x] `GitConfig` + `ELKConfig` — centralised settings for Git provider and ELK cluster
- [x] `GitPRMetadata` model — pr_number, pr_title, pr_url, state, merged, merge_commit_sha, head/base branch, author, timestamps
- [x] `GitClient` — GitHub / GitLab / Bitbucket REST API client; `get_pr`, `get_pr_by_branch`, `detect_pr_refs`, `fetch_prs_for_issue`
- [x] `LogLookupService` extended — ELK backend with `search_elk_logs`, Elasticsearch Query DSL builder, response parser, API-key + Basic auth
- [x] `ContextCollector` — `_fetch_git_prs` (PR ref detection from issue + comments), `_fetch_elk_logs` (summary-based ELK query); `git_prs` + `elk_log_entries` in `ContextCollectionResult`
- [x] Drafter — `_format_pr_evidence`, `_format_elk_preview`; PR + ELK in existing evidence, citations, and evidence_used; CANNOT_REPRODUCE + FIXED_VALIDATE templates surface PR commit + branch
- [x] `app.py` — `GitClient` wired in, health endpoint now reports `git` + `elk` integration status; version bumped to 0.6.0
- [x] Tests — `test_git_client.py` (37 tests), `test_elk_lookup.py` (19 tests)
- [x] **Final:** 352 unit + integration tests passing, 81% code coverage

## Architecture

```
Jira Cloud (Webhook)
    │
    ▼
POST /webhook/jira  ──▶  EventFilter (type, status, keywords, idempotency)
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

| Bucket | Trigger keywords |
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
# 1. Create & activate venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env
# Edit .env with your Jira credentials (Copilot SDK key is optional)

# 4. Run tests
pytest

# 5. Start server
uvicorn src.api.app:app --reload --port 8000

# 6. Health check
curl http://localhost:8000/health
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — Component design and data flow
- [Setup Guide](docs/SETUP.md) — Installation, configuration, and testing
