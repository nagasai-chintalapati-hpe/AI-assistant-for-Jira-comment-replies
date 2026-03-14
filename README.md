# AI Assistant for Jira Comment Replies

> Automatically drafts context-aware replies to Jira defect comments — with human review before anything is posted.

A **FastAPI** service that listens for Jira webhook events, classifies engineer comments into one of eight intent buckets, collects corroborating evidence from TestRail, Git, Jenkins/ELK, Confluence, and S3, then drafts a structured reply for a human to approve or edit before it reaches Jira.

**Authors:** Nagasai Chintalapati · Yousef Konswah — HPE Intern Project 2026

---

## Why?

| Without the assistant | With the assistant |
|---|---|
| Engineer manually opens TestRail, Git, and ELK to triage a comment | Webhook fires — context collected in under 5 s |
| Copy-pastes test case IDs and log lines by hand | Draft pre-populated with clickable evidence links |
| Reply quality varies by person and shift | Consistent tone and structure on every defect |
| No audit trail of what evidence informed the reply | Every draft stored in SQLite + optional Jira custom field |
| Only works when someone is at a desk | 24x7 first-pass coverage |

---

## Features

| Feature | Detail |
|---|---|
| 8-bucket classification | Cannot Reproduce, Need More Info, Fixed-Validate, By Design, Duplicate, Blocked, Config Issue, Other |
| Multi-source context | TestRail, Git PRs, Jenkins logs, ELK search, Confluence KB, S3 artifacts |
| Dual-strategy drafting | GitHub Copilot SDK (GPT-4o) with keyword-heuristic fallback |
| RAG prior-defect search | ChromaDB KB query + `source=jira` prior-defect query, deduplicated |
| Author-role inference | QA, DevOps, Developer detected from display name / email |
| Human-in-the-loop UI | `/ui` review dashboard — approve, edit inline, or reject |
| Teams AdaptiveCard | Notification with Approve / Reject buttons and a Review link |
| Jira audit field | Approved draft written to `JIRA_DRAFT_FIELD_ID` before posting |
| Webhook security | HMAC-SHA256 signature validation + per-IP rate limiting |
| Test coverage | 367 unit tests, 0 failures |

---

## Architecture

```
Internet / SaaS
  Jira Cloud --> Webhook Relay (ngrok / HPE relay)
  GitHub, TestRail, Microsoft Teams
         |
         | HTTPS / TLS
         v
  nginx --> FastAPI :8000
              |-- Event Filter     (dedup, bot guard)
              |-- Classifier       (Copilot SDK -> keyword fallback)
              |-- Context Collector
              |     |-- JiraConnector       (REST API)
              |     |-- TestRailConnector   (REST API)
              |     |-- GitConnector        (REST API)
              |     |-- LogStoreConnector   (ELK / Jenkins)
              |     |-- S3ArtifactFetcher   (S3 / MinIO)
              |     `-- Confluence/PDFConn  (RAG)
              |-- Drafter          (LLM -> heuristic fallback)
              |-- Review UI        (/ui)
              `-- Notifier         (Teams / email)

On-Prem Data
  SQLite, ChromaDB, S3/MinIO, Redis (opt.), RabbitMQ (opt.)
```

Full topology: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Pipeline

1. **Webhook** — Jira fires `comment_created`; Event Filter checks type, status, and deduplication
2. **Classify** — bucket + confidence score (0-1) via Copilot SDK or keyword rules
3. **Collect context** — parallel fan-out: TestRail, Git, ELK, Jenkins, S3, RAG
4. **Infer author role** — QA / DevOps / Developer from display name / email
5. **Draft** — template filled with evidence, optionally refined by LLM
6. **Review** — engineer approves, edits, or rejects via `/ui` or Teams card
7. **Post** — on approve: write audit field then post comment to Jira

Draft structure: Acknowledge, Evidence, Repro steps, Missing info, Next action

---

## Classification Buckets

| Bucket | Type value | Example phrases |
|---|---|---|
| Cannot Reproduce | `cannot_reproduce` | "cannot repro", "works on my machine" |
| Need More Info | `need_more_info` | "need logs", "provide stack trace" |
| Fixed — Validate | `fixed_validate` | "fix merged", "please validate in build" |
| By Design | `by_design` | "as designed", "expected behaviour" |
| Duplicate / Fixed | `duplicate_fixed` | "duplicate of", "already fixed in" |
| Blocked / Waiting | `blocked_waiting` | "blocked by", "waiting on upstream" |
| Configuration Issue | `configuration_issue` | "config error", "misconfigured" |
| Other (fallback) | `other` | anything below minimum keyword score |

---

## Quick Start

```bash
# 1. Clone and set up virtualenv
git clone <repo-url> && cd AI-assistant-for-Jira-comment-replies
python -m venv .venv && source .venv/bin/activate

# 2. Install
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env
# Minimum required: JIRA_BASE_URL, JIRA_USERNAME, JIRA_API_TOKEN

# 4. Start
uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

# 5. Open the review UI
open http://localhost:8000/ui
```

**Docker (production)**

```bash
docker-compose up -d
```

**Expose webhook to Jira Cloud**

```bash
ngrok http 8000
# Set Jira webhook URL to https://<id>.ngrok.io/webhook/jira
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/webhook/jira` | Receive Jira comment events |
| GET | `/ui` | Draft review dashboard |
| POST | `/ui/drafts/{id}/approve` | Approve and post to Jira |
| POST | `/ui/drafts/{id}/reject` | Reject with optional feedback |
| GET | `/drafts` | List all drafts (JSON) |
| GET | `/drafts/{id}` | Get a single draft |
| POST | `/rag/ingest/confluence` | Ingest Confluence spaces |
| POST | `/rag/ingest/pdf` | Ingest a PDF file |
| POST | `/rag/ingest/jira` | Ingest resolved tickets as prior-defect context |
| GET | `/rag/search` | Ad-hoc semantic search |
| GET | `/health` | Liveness check |

---

## Confidence Levels

| Level | Score | Meaning |
|---|---|---|
| HIGH | > 0.80 | Strong match — bucket is clear |
| MEDIUM | 0.50 - 0.80 | Some signal but ambiguous — reviewer should confirm |
| LOW | < 0.50 | Weak match — falls to `other`, manual review recommended |

Draft acceptance rates per bucket are logged in SQLite (`GET /metrics`).

---

## Review UI

Open `http://localhost:8000/ui` after starting the server.

Each draft card shows the Jira ticket key, author + inferred role, classification badge, confidence score, evidence summary, and the full draft text. The draft is editable inline before approving.

- **Approve** — writes to `JIRA_DRAFT_FIELD_ID` (if set) then posts as a Jira comment
- **Reject** — dismisses the draft and stores feedback

When `TEAMS_WEBHOOK_URL` is configured, a card is posted to Teams for every new draft with Review, Approve, and Reject action buttons.

---

## RAG Knowledge Base

```bash
# Ingest Confluence (set CONFLUENCE_SPACES=TEAM,KB in .env)
curl -X POST http://localhost:8000/rag/ingest/confluence

# Ingest a PDF runbook
curl -X POST http://localhost:8000/rag/ingest/pdf -F "file=@runbook.pdf"

# Ingest resolved Jira tickets as prior-defect context
curl -X POST http://localhost:8000/rag/ingest/jira

# Ad-hoc query
curl -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "login timeout after firmware upgrade", "top_k": 5}'
```

At query time, two ChromaDB queries run in parallel:

1. **KB query** — Confluence pages + PDFs
2. **Prior-defect query** — resolved Jira tickets (`source=jira` filter)

Results are deduplicated by `chunk_id` before injection into the draft prompt.

---

## Configuration

Copy `.env.example` to `.env`.

### Core

| Variable | Default | Description |
|---|---|---|
| `APP_PORT` | `8000` | Listen port |
| `APP_BASE_URL` | `http://localhost:8000` | Public URL for Teams card links |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `ASSISTANT_DB_PATH` | `.data/assistant.db` | SQLite path |

### Jira

| Variable | Description |
|---|---|
| `JIRA_BASE_URL` | `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Service-account email |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_WEBHOOK_SECRET` | HMAC secret — set `VALIDATE_WEBHOOK_SIGNATURE=true` in prod |
| `JIRA_DRAFT_FIELD_ID` | Optional custom field (e.g. `customfield_10200`) for audit storage |

### LLM / Copilot

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `copilot` | `copilot` / `local` / `none` |
| `COPILOT_API_KEY` | — | GitHub Copilot API key |
| `COPILOT_MODEL` | `gpt-4o` | Model name |
| `LLM_MODEL_PATH` | — | Path to `.gguf` file (local backend only) |

### RAG

| Variable | Default | Description |
|---|---|---|
| `CHROMA_PERSIST_DIR` | `.data/chroma` | ChromaDB storage path |
| `RAG_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model |
| `RAG_TOP_K` | `5` | Snippets retrieved per query |

### Integrations

| Variable | Description |
|---|---|
| `TESTRAIL_BASE_URL` / `TESTRAIL_API_KEY` | TestRail instance and API key |
| `GIT_PROVIDER` / `GIT_TOKEN` | `github` / `gitlab` / `bitbucket` + PAT |
| `ELK_HOST` / `ELK_API_KEY` | Elasticsearch / OpenSearch |
| `JENKINS_BASE_URL` / `JENKINS_API_TOKEN` | Jenkins instance |
| `CONFLUENCE_BASE_URL` / `CONFLUENCE_SPACES` | Confluence + space keys to ingest |
| `S3_BUCKET` / `S3_ENDPOINT_URL` | S3 or MinIO bucket |
| `TEAMS_WEBHOOK_URL` | Teams incoming webhook for AdaptiveCard notifications |

---

## Running Tests

```bash
pytest tests/unit/ -v

# With coverage report
pytest tests/unit/ --cov=src --cov-report=html && open htmlcov/index.html

# Integration tests (requires live Jira + TestRail credentials)
pytest tests/integration/ -v
```

---

## Project Structure

```
src/
|-- api/
|   |-- app.py                # FastAPI routes
|   `-- event_filter.py       # Dedup + bot guard
|-- agent/
|   |-- classifier.py         # 8-bucket classifier
|   |-- context_collector.py  # Fan-out to all integrations + RAG
|   `-- drafter.py            # Draft generation
|-- integrations/
|   |-- jira.py  testrail.py  git.py
|   |-- log_lookup.py  confluence.py
|   `-- notifications.py  s3_connector.py
|-- rag/
|   |-- engine.py             # ChromaDB dual-query
|   `-- ingest.py             # Confluence / PDF / Jira ingestion
|-- models/                   # Pydantic models
|-- storage/sqlite_store.py   # Draft persistence + metrics
`-- config.py                 # All settings (env vars)
```

---

## Troubleshooting

**Webhook returns 400 (signature mismatch)**
Set `VALIDATE_WEBHOOK_SIGNATURE=false` for local dev, or ensure `JIRA_WEBHOOK_SECRET` matches the Jira webhook config.

**ChromaDB collection empty after ingest**
Check `CHROMA_PERSIST_DIR` is writable and `CONFLUENCE_SPACES` has at least one valid space key, then re-run `POST /rag/ingest/confluence`.

**Drafts have confidence 0.0**
`LLM_BACKEND` is `none` or `COPILOT_API_KEY` is missing — the keyword fallback does not produce a confidence score. Set `LLM_BACKEND=copilot` and add the API key.

**Teams card not appearing**
Confirm `TEAMS_WEBHOOK_URL` is an Incoming Webhook URL and the connector is enabled on the Teams channel.

---

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — deployment topology, pipeline detail, security
- [docs/SETUP.md](docs/SETUP.md) — step-by-step environment and integration setup

---

*Internal HPE use. Not for external distribution.*
