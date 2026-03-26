# AI Assistant for Jira Comment Replies

> Drafts context-aware replies to Jira defect comments — with human review before anything is posted.

A **FastAPI** service that listens for Jira webhooks, classifies comments into intent buckets, collects evidence from TestRail, Git, Jenkins, Confluence, and S3, then drafts a structured reply for human approval.

**Authors:** Nagasai Chintalapati · Yousef Konswah · Vinnarasu Ganesan — HPE 2026

---

## Why

| Without | With |
|---------|------|
| Manually open TestRail, Git, ELK to triage | Context collected in < 5 s |
| Copy-paste test IDs and log lines by hand | Draft pre-populated with evidence links |
| Reply quality varies by person/shift | Consistent tone and structure |
| No audit trail | Every draft stored with full evidence chain |
| Only works when someone is at a desk | 24×7 first-pass coverage |

---

## Features

- **8-bucket classification** — Cannot Reproduce, Need More Info, Fixed-Validate, By Design, Duplicate, Blocked, Config Issue, Other
- **Multi-source context** — TestRail, Git PRs, Jenkins logs, ELK, Confluence KB, S3 artifacts
- **RAG knowledge base** — ChromaDB semantic search over Confluence, PDFs, and resolved Jira tickets
- **Multi-repo PR search** — fan-out across configured repos
- **Duplicate detection** — Jaccard similarity against past drafts on the same issue
- **Systemic pattern alerts** — red alert when 3+ open bugs share the same component/version
- **Review UI** — `/ui` dashboard: approve, edit inline, reject, rate 1–5 stars
- **Analytics dashboard** — `/dashboard` with KPIs, charts, severity challenge log
- **Teams notifications** — AdaptiveCard with Approve/Reject buttons
- **Webhook security** — HMAC-SHA256 + per-IP rate limiting

---

## Quick Start

```bash
# Clone and install
git clone git@github.com:nagasai-chintalapati-hpe/AI-assistant-for-Jira-comment-replies.git && cd AI-assistant-for-Jira-comment-replies

# Configure
cp .env.example .env
# Set: JIRA_BASE_URL, JIRA_USERNAME, JIRA_API_TOKEN

# Run
docker compose up -d --build

# Open
open http://localhost:8000/ui
```

---

## Architecture

```
Jira Cloud ──webhook──▶ Event Filter ──▶ Classifier ──▶ Context Collector
                                                              │
     ┌────────────────────────────────────────────────────────┘
     ▼
Duplicate Detector ──▶ Pattern Detector ──▶ Drafter ──▶ Review UI
                                                         │
                                               ┌─────────┴────────┐
                                               ▼                  ▼
                                           Approve            Reject
                                        (post to Jira)    (store feedback)
```

Full details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## API

### Core

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/webhook/jira` | Receive Jira comment events |
| POST | `/approve` | Approve draft (JSON API) |
| POST | `/reject` | Reject draft (JSON API) |
| GET | `/drafts` | List drafts |
| GET | `/drafts/{id}` | Get a single draft |

### Review UI

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/ui` | Draft review dashboard |
| GET | `/ui/drafts/{id}` | Draft detail + evidence panel |
| POST | `/ui/drafts/{id}/approve` | Approve and post to Jira |
| POST | `/ui/drafts/{id}/reject` | Reject with feedback |
| POST | `/ui/drafts/{id}/rate` | Rate 1–5 stars |

### Dashboard

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/dashboard` | Analytics dashboard |
| GET | `/dashboard/api/summary` | KPI summary |
| GET | `/dashboard/api/daily-volume` | Daily draft volume |
| GET | `/dashboard/api/classifications` | Classification breakdown |
| GET | `/dashboard/api/severity` | Severity challenge log |
| GET | `/dashboard/api/top-issues` | Most active issues |
| GET | `/dashboard/api/repos` | Multi-repo PR stats |
| GET | `/dashboard/api/response-time` | Pipeline latency trend |

### RAG

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/rag/ingest/pdf` | Ingest a PDF |
| POST | `/rag/ingest/text` | Ingest raw text |
| POST | `/rag/ingest/confluence` | Ingest Confluence pages |
| POST | `/rag/ingest/jira` | Ingest resolved Jira tickets |
| GET | `/rag/search` | Semantic search (query params) |
| POST | `/rag/query` | Semantic search (JSON body) |
| GET | `/rag/stats` | Collection stats |
| DELETE | `/rag/document/{title}` | Remove a source document |

### Ops

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check |
| GET | `/health/deep` | Readiness check (all integrations) |
| GET | `/metrics` | Draft quality metrics (JSON) |
| GET | `/metrics/prometheus` | Prometheus-format metrics |
| POST | `/admin/drafts/purge-stale` | Purge old unactioned drafts |

---

## Configuration

Copy `.env.example` to `.env`. See [docs/SETUP.md](docs/SETUP.md) for the full variable reference.

**Minimum required:**

```env
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_USERNAME=your-email@company.com
JIRA_API_TOKEN=your-api-token
```

**Optional extras:**

```bash
pip install -e ".[redis]"          # Redis rate-limiter
pip install -e ".[queue]"          # RabbitMQ async queue
pip install -e ".[s3]"             # S3 / MinIO artifact fetcher
pip install -e ".[observability]"  # Prometheus metrics
pip install -e ".[prod]"           # All of the above
```

---

## Tests

```bash
pytest tests/unit/ -v                # unit tests
pytest tests/unit/ --cov=src         # with coverage
pytest tests/integration/ -v         # requires live credentials
```

---

## Project Structure

```
src/
├── api/
│   ├── app.py              # FastAPI application + lifespan
│   ├── orchestrator.py     # Pipeline orchestration + pattern detection
│   ├── event_filter.py     # Dedup + bot guard
│   ├── security.py         # HMAC validation + rate limiting
│   ├── deps.py             # Singleton dependencies
│   └── routes/
│       ├── webhook.py      # POST /webhook/jira
│       ├── drafts.py       # JSON draft API
│       ├── rag.py          # RAG ingest + search
│       ├── health.py       # Health + metrics
│       ├── admin.py        # Admin operations
│       ├── ui.py           # Review UI routes
│       └── dashboard.py    # Analytics dashboard
├── agent/
│   ├── classifier.py       # 8-bucket classifier
│   ├── context_collector.py# Multi-source evidence collection
│   ├── drafter.py          # Draft generation
│   ├── duplicate_detector.py # Jaccard similarity check
│   ├── severity_challenger.py # Rovo severity evaluation
│   └── pipeline_correlator.py # Cross-pipeline failure detection
├── integrations/
│   ├── jira.py             # Jira REST API
│   ├── testrail.py         # TestRail API
│   ├── git.py              # GitHub / GitLab / Bitbucket
│   ├── confluence.py       # Confluence API
│   ├── jenkins.py          # Jenkins API
│   ├── log_lookup.py       # Jenkins console + ELK logs
│   ├── s3_connector.py     # S3 / MinIO artifacts
│   └── notifications.py    # Teams + Email
├── rag/
│   ├── engine.py           # ChromaDB vector search
│   └── ingest.py           # PDF / Confluence / text / Jira ingestion
├── models/                 # Pydantic models
├── storage/
│   └── sqlite_store.py     # Draft persistence + metrics
├── llm/
│   └── client.py           # Copilot SDK / local LLM client
├── utils/
│   └── redactor.py         # PII redaction
└── config.py               # Environment-based settings
```

---

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — deployment topology, pipeline, components
- [docs/SETUP.md](docs/SETUP.md) — installation, configuration, deployment

---

*Internal HPE use. Not for external distribution.*
