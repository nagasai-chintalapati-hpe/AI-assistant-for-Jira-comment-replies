# AI Assistant for Jira Comment Replies

> Automatically generates context-aware draft replies to Jira defect comments — classifying intent, gathering evidence from Jira, TestRail, Git, Confluence, and Jenkins, then routing drafts through a human-approval UI before posting back.

**Intern Project 2026 · Nagasai & Yousef**

---

## Features

| Capability | Detail |
|---|---|
| **Webhook ingestion** | Receives Jira comment events; filters by issue type, status, and idempotency |
| **Comment classification** | 8 intent buckets via keyword heuristics with optional Copilot SDK fallback |
| **Context collection** | Jira fields & history · TestRail run summaries · Git PR metadata · Jenkins/ELK logs · Confluence KB · RAG semantic search |
| **Draft generation** | Template-per-bucket enriched with evidence, citations, suggested labels & next actions |
| **Human approval UI** | Review, edit, approve or reject at `GET /ui` before any reply posts to Jira |
| **Notifications** | Teams webhook cards + SMTP email on draft created, approved, or rejected |
| **Persistent storage** | SQLite draft store (WAL mode) with full audit trail |
| **LLM backends** | `none` (templates only) · `copilot` (GitHub Copilot SDK) · `local` (llama.cpp / GGUF) |

## Architecture

```
Jira Cloud ──▶ POST /webhook/jira
                    │
               EventFilter
          (type · status · idempotency)
                    │
           CommentClassifier
          (8 buckets · keyword + LLM)
                    │
           ContextCollector
   Jira · TestRail · Git · Jenkins/ELK
       Confluence · RAG (ChromaDB)
                    │
           ResponseDrafter
    template + evidence + citations
                    │
            SQLite Store
      /drafts · /approve · /reject
                    │
          Approval UI  ──▶  Jira
                    │
           Notifications
        Teams card · SMTP email
```

## Classification Buckets

| Bucket | Trigger keywords |
|---|---|
| `cannot_reproduce` | "cannot reproduce", "can't repro", "works on my machine" |
| `need_more_info` | "need logs", "provide logs", "need more info", "more details" |
| `fixed_validate` | "fix ready", "fix deployed", "please validate", "already fixed" |
| `by_design` | "as designed", "by design", "expected behavior", "working as intended" |
| `duplicate_fixed` | "duplicate", "already fixed", "closed in" |
| `blocked_waiting` | "waiting on", "blocked by", "dependency" |
| `config_issue` | "misconfiguration", "config issue", "wrong setting", "environment" |
| `other` | *(fallback)* |

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

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — Component design and data flow
- [Setup Guide](docs/SETUP.md) — Installation, configuration, and deployment
