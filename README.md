# 🤖 AI Assistant for Jira Comment Replies

> **Automatically drafts context-aware replies to Jira defect comments — with human review before anything is posted.**

A FastAPI service that listens for Jira webhook events, classifies engineer comments into one of eight intent buckets, gathers corroborating evidence from TestRail, Git, Jenkins/ELK logs, Confluence, and S3 artifacts, then drafts a structured reply for a human reviewer to approve or edit before it reaches Jira.

**Authors:** Nagasai Chintalapati · Yousef Konswah — HPE Intern Project 2025

---

## Why?

| Without the assistant | With the assistant |
|---|---|
| Triage engineer reads the comment, manually opens TestRail, Git, ELK | Webhook fires → context collected in < 5 s |
| Copy-pastes test case IDs, build numbers, log lines by hand | Draft pre-populated with clickable evidence links |
| Reply quality varies by engineer and shift | Consistent tone + structure on every defect |
| No audit trail of which evidence informed the reply | Every draft stored in SQLite + optional Jira custom field |
| Works only when someone is at a desk | 24 × 7 first-pass coverage |

---

## Features

| Feature | Detail |
|---|---|
| **8-bucket classification** | Cannot Reproduce · Need More Info · Fixed–Validate · By Design · Duplicate/Already Fixed · Blocked/Waiting · Configuration Issue · Other |
| **Multi-source context** | TestRail cases, Git commits/PRs, Jenkins build logs, ELK log search, Confluence KB, S3 artifacts |
| **Dual-strategy drafting** | GitHub Copilot SDK (GPT-4o) with keyword-heuristic fallback |
| **RAG prior-defect search** | ChromaDB KB query + separate `source=jira` prior-defect query, deduplicated |
| **Author-role inference** | QA · DevOps · Developer detected from display name / e-mail — tailors draft tone |
| **Human-in-the-loop UI** | `/ui` review dashboard — approve, edit inline, or reject before any comment is posted |
| **Teams AdaptiveCard** | Notification with direct Approve / Reject buttons and a Review link |
| **Jira custom field audit** | Approved draft optionally written to `JIRA_DRAFT_FIELD_ID` before posting |
| **HMAC-SHA256 webhook validation** | Configurable per-IP rate limiting (60 req/min default) |
| **367 unit tests** | 100 % spec-compliant, 0 failures |

---

## Architecture

\`\`\`
+-----------------------------------------------------------------------------------+
|  INTERNET / SAAS LAYER                                                            |
|                                                                                   |
|   Jira Cloud ──── comment webhook ────►  Webhook Relay (ngrok / HPE relay)       |
|   GitHub / GitLab                                                                 |
|   TestRail SaaS                                                                   |
|   Microsoft Teams                                                                 |
+-----------------------------------------|-----------------------------------------+
                                          │  HTTPS / TLS
+-----------------------------------------▼-----------------------------------------+
|  DMZ / PERIMETER                                                                   |
|                                                                                    |
|   Reverse Proxy (nginx)  ──►  FastAPI   :8000                                     |
|                                │                                                   |
|                                ├── Event Filter (deduplicate, ignore bot comments) |
|                                ├── Classifier  (Copilot SDK → keyword fallback)   |
|                                ├── Context Collector                               |
|                                │     ├── JiraConnector       (REST API)            |
|                                │     ├── TestRailConnector   (REST API)            |
|                                │     ├── GitConnector        (REST API)            |
|                                │     ├── LogStoreConnector   (ELK / Jenkins)       |
|                                │     ├── S3ArtifactFetcher   (S3 / MinIO)          |
|                                │     └── Confluence/PDFConn  (RAG)                 |
|                                ├── Drafter   (LLM → heuristic fallback)            |
|                                ├── Review UI (/ui)                                 |
|                                └── Notifier  (Teams AdaptiveCard / e-mail)        |
+-----------------------------------------------------------------------------------+
|  ON-PREM DATA LAYER                                                                |
|                                                                                    |
|   SQLite (.data/assistant.db)   ChromaDB (.data/chroma)   S3 / MinIO              |
|   Redis (opt. rate-limit cache) RabbitMQ (opt. queue)                             |
+-----------------------------------------------------------------------------------+
\`\`\`

Full topology detail → [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## End-to-End Pipeline

\`\`\`
1. Jira comment webhook  ──►  Event Filter (bot/self check, dedup)
        │
        ▼
2. Classifier            ──►  bucket + confidence (0–1)
        │
        ▼
3. Context Collector     ──►  parallel fan-out
        ├── TestRail  : matching test cases (semantic similarity)
        ├── Git       : recent commits / open PRs for the component
        ├── ELK       : error-log snippets for the last 24 h
        ├── Jenkins   : build log for the triggering build
        ├── S3        : test-result artifacts / screenshots
        └── RAG       : KB articles + prior similar defects (ChromaDB)
        │
        ▼
4. Author-role inference ──►  "QA" | "DevOps" | "Developer"
        │
        ▼
5. Drafter               ──►  structured reply draft
        │   ✅ Acknowledge  · 🔎 Evidence  · 🧪 Repro  · ❓ Missing  · ▶️ Next action
        ▼
6. Review UI / Teams card ──► engineer: Approve · Edit · Reject
        │
        ▼
7. On Approve            ──►  write JIRA_DRAFT_FIELD_ID (audit) → post comment to Jira
\`\`\`

---

## Classification Buckets

| Bucket | `CommentType` | Typical trigger phrases |
|---|---|---|
| Cannot Reproduce | `cannot_reproduce` | "cannot repro", "works on my machine" |
| Need More Info | `need_more_info` | "need logs", "provide stack trace" |
| Fixed — Validate | `fixed_validate` | "fix merged", "please validate in build" |
| By Design | `by_design` | "as designed", "expected behaviour" |
| Duplicate / Already Fixed | `duplicate_fixed` | "duplicate of", "already fixed in" |
| Blocked / Waiting | `blocked_waiting` | "blocked by", "waiting on upstream" |
| Configuration Issue | `configuration_issue` | "config error", "misconfigured" |
| Other (fallback) | `other` | anything below minimum keyword score |

Primary strategy: **GitHub Copilot SDK** (structured JSON response).
Fallback strategy: **keyword heuristics** (no external API needed).

---

## Quick Start

\`\`\`bash
# 1. Clone and create virtualenv
git clone <repo-url> && cd AI-assistant-for-Jira-comment-replies
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env
# edit .env — minimum required: JIRA_BASE_URL, JIRA_USERNAME, JIRA_API_TOKEN

# 4. Ingest your knowledge base (optional but recommended)
curl -X POST http://localhost:8000/rag/ingest/confluence
curl -X POST http://localhost:8000/rag/ingest/pdf        # after uploading PDFs

# 5. Start the server
uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

# 6. Open the review UI
open http://localhost:8000/ui
\`\`\`

### Docker (production)

\`\`\`bash
docker-compose up -d
# server starts on :8000, data persisted in ./data/
\`\`\`

### Expose webhook to Jira Cloud

\`\`\`bash
# ngrok (dev)
ngrok http 8000
# configure Jira webhook URL → https://<id>.ngrok.io/webhook/jira

# HPE relay (prod) — set APP_BASE_URL to the relay endpoint
\`\`\`

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/webhook/jira` | `POST` | Receive Jira comment events |
| `/ui` | `GET` | Draft review dashboard |
| `/approve` | `POST` | Approve a draft — posts comment to Jira |
| `/reject` | `POST` | Reject a draft — marks it dismissed |
| `/drafts` | `GET` | List all pending drafts (JSON) |
| `/drafts/{id}` | `GET` | Get a single draft |
| `/rag/ingest/confluence` | `POST` | Ingest Confluence spaces into ChromaDB |
| `/rag/ingest/pdf` | `POST` | Ingest an uploaded PDF into ChromaDB |
| `/rag/ingest/jira` | `POST` | Ingest resolved Jira tickets as prior-defect context |
| `/rag/query` | `POST` | Ad-hoc RAG query |
| `/health` | `GET` | Liveness check |

---

## Confidence Levels

Every draft includes a `confidence` score (0–1) derived from classifier certainty and evidence coverage.

### Classifier Confidence

| Level | Score | Meaning |
|---|---|---|
| 🟢 **HIGH** | > 0.80 | Strong keyword or Copilot SDK match — bucket is clear |
| 🟡 **MEDIUM** | 0.50 – 0.80 | Some signal but ambiguous phrasing — reviewer should confirm bucket |
| 🔴 **LOW** | < 0.50 | Falls through to `other` — manual classification recommended |

### Draft Acceptance Rate

The system logs `approved` / `rejected` outcomes per `CommentType` in SQLite.
A low acceptance rate on a specific bucket indicates the drafting heuristics need tuning.

---

## Review UI

Navigate to `http://localhost:8000/ui` after starting the server.

- **Pending** tab: all unreviewed drafts
- Each card shows: Jira ticket key, comment author + role, bucket badge, confidence bar, evidence summary, and the full draft text
- Inline edit — modify the draft before approving
- **Approve** → writes draft to Jira custom field (if `JIRA_DRAFT_FIELD_ID` set) then posts as a Jira comment
- **Reject** → dismisses the draft, logs the decision

### Teams AdaptiveCard

When `TEAMS_WEBHOOK_URL` is configured, a card is posted for every new draft:

\`\`\`
[PCBE-1234] Cannot Reproduce — confidence 0.87
─────────────────────────────────────────
Evidence: 3 TestRail cases · 2 Git commits · 12 ELK hits
─────────────────────────────────────────
[ 👁 Review ]   [ ✅ Approve ]   [ ❌ Reject ]
\`\`\`

---

## RAG Knowledge Base

\`\`\`bash
# Ingest Confluence spaces (set CONFLUENCE_SPACES=TEAM,KB in .env)
curl -X POST http://localhost:8000/rag/ingest/confluence

# Ingest a PDF runbook
curl -X POST http://localhost:8000/rag/ingest/pdf \
     -F "file=@runbook.pdf"

# Ingest resolved Jira tickets as prior-defect context
curl -X POST http://localhost:8000/rag/ingest/jira

# Ad-hoc query
curl -X POST http://localhost:8000/rag/query \
     -H "Content-Type: application/json" \
     -d '{"query": "login timeout after firmware upgrade", "top_k": 5}'
\`\`\`

At query time, the Context Collector runs **two parallel** ChromaDB queries:
1. **KB query** — general knowledge base (Confluence + PDFs)
2. **Prior-defect query** — `where={"source": "jira"}` filtered to resolved tickets with similar symptoms

Results are deduplicated by `chunk_id` and the top-k merged snippets are injected into the draft prompt.

---

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and fill in the values you need.

#### Core

| Variable | Default | Description |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Bind address |
| `APP_PORT` | `8000` | Listen port |
| `APP_BASE_URL` | `http://localhost:8000` | Public URL (used in Teams card links) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `MAX_COMMENTS` | `10` | Max comment history fetched per ticket |
| `ASSISTANT_DB_PATH` | `.data/assistant.db` | SQLite database path |

#### Jira

| Variable | Description |
|---|---|
| `JIRA_BASE_URL` | `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Service-account e-mail |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_WEBHOOK_SECRET` | HMAC secret for signature validation |
| `VALIDATE_WEBHOOK_SIGNATURE` | `true` in production (default `false`) |
| `JIRA_DRAFT_FIELD_ID` | Optional — custom field ID (e.g. `customfield_10200`) to store draft for auditability |

#### LLM / Copilot

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `copilot` | `copilot` \| `local` \| `none` |
| `COPILOT_API_KEY` | — | GitHub Copilot API key |
| `COPILOT_MODEL` | `gpt-4o` | Model name |
| `COPILOT_TEMPERATURE` | `0.1` | Generation temperature |
| `LLM_MODEL_PATH` | — | Path to `.gguf` file (local backend only) |

#### RAG / ChromaDB

| Variable | Default | Description |
|---|---|---|
| `CHROMA_PERSIST_DIR` | `.data/chroma` | ChromaDB storage path |
| `RAG_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model |
| `RAG_TOP_K` | `5` | Snippets retrieved per query |
| `RAG_CHUNK_SIZE` | `500` | Token chunk size |

#### Integrations

| Variable | Description |
|---|---|
| `TESTRAIL_BASE_URL` | TestRail instance URL |
| `TESTRAIL_USERNAME` | TestRail username |
| `TESTRAIL_API_KEY` | TestRail API key |
| `TESTRAIL_PROJECT_ID` | Project to search |
| `GIT_PROVIDER` | `github` \| `gitlab` \| `bitbucket` |
| `GIT_BASE_URL` | Override for self-hosted Git |
| `GIT_TOKEN` | Personal access token |
| `ELK_HOST` | Elasticsearch / OpenSearch host |
| `ELK_USERNAME` / `ELK_PASSWORD` | Basic auth (or use `ELK_API_KEY`) |
| `ELK_INDEX_PATTERN` | Index alias (default `logs-*`) |
| `JENKINS_BASE_URL` | Jenkins instance URL |
| `JENKINS_API_TOKEN` | Jenkins API token |
| `CONFLUENCE_BASE_URL` | Confluence instance URL |
| `CONFLUENCE_SPACES` | Comma-separated space keys to ingest |
| `S3_BUCKET` | S3 / MinIO artifact bucket |
| `S3_ENDPOINT_URL` | Leave blank for AWS default |

#### Notifications

| Variable | Description |
|---|---|
| `TEAMS_WEBHOOK_URL` | Incoming Webhook URL for AdaptiveCard delivery |
| `SMTP_HOST` / `SMTP_PORT` | SMTP relay for e-mail notifications |
| `EMAIL_FROM` / `EMAIL_TO` | Sender and recipient addresses |

#### Infrastructure (optional)

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Per-IP rate limiting on `/webhook/jira` |
| `RATE_LIMIT_RPM` | `60` | Max requests per minute per IP |
| `REDIS_ENABLED` | `false` | Use Redis for distributed rate-limit state |
| `REDIS_URL` | — | Full Redis URL (overrides host/port) |
| `QUEUE_ENABLED` | `false` | Route webhook events through RabbitMQ |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost/` | AMQP connection URL |

---

## Running Tests

\`\`\`bash
# All unit tests
pytest tests/unit/ -v

# With coverage
pytest tests/unit/ --cov=src --cov-report=html
open htmlcov/index.html

# Integration tests (requires live Jira + TestRail)
pytest tests/integration/ -v
\`\`\`

367 tests · 0 failures.

---

## Project Structure

\`\`\`
src/
├── api/
│   ├── app.py            # FastAPI routes: /webhook, /ui, /approve, /reject, /rag/*
│   └── event_filter.py   # Dedup + bot-comment guard
├── agent/
│   ├── classifier.py     # 8-bucket classifier (Copilot SDK + keyword fallback)
│   ├── context_collector.py  # Fan-out to all integrations + RAG
│   └── drafter.py        # Draft generation (LLM + heuristic fallback)
├── integrations/
│   ├── jira.py           # Jira REST client
│   ├── testrail.py       # TestRail REST client
│   ├── git.py            # GitHub / GitLab / Bitbucket client
│   ├── log_lookup.py     # Jenkins + local log reader
│   ├── confluence.py     # Confluence REST client
│   ├── notifications.py  # Teams AdaptiveCard + SMTP
│   └── ...
├── rag/
│   ├── engine.py         # ChromaDB query (KB + prior-defect dual-query)
│   └── ingest.py         # Confluence / PDF / Jira ingestion
├── models/               # Pydantic models: Comment, Classification, Context, Draft
├── storage/
│   └── sqlite_store.py   # Draft persistence + outcome logging
└── config.py             # All settings via env vars (frozen dataclasses)
\`\`\`

---

## Troubleshooting

### Webhook returns 400 — signature mismatch

\`\`\`
VALIDATE_WEBHOOK_SIGNATURE=false   # disable for local dev
\`\`\`

For production, make sure `JIRA_WEBHOOK_SECRET` matches the secret set in the
Jira webhook configuration screen.

### ChromaDB collection not found after ingest

\`\`\`bash
# Re-ingest
curl -X POST http://localhost:8000/rag/ingest/confluence
\`\`\`

Check `CHROMA_PERSIST_DIR` is writable and that `CONFLUENCE_SPACES` contains at least one valid space key.

### Draft posted empty / confidence 0.0

`LLM_BACKEND=none` or Copilot API key missing → keyword fallback fires.
Confidence is intentionally 0.0 when using the heuristic fallback.
Set `LLM_BACKEND=copilot` and provide `COPILOT_API_KEY` for scored drafts.

### Teams card not appearing

Verify `TEAMS_WEBHOOK_URL` is the **Incoming Webhook** URL (not a bot URL) and
that the Teams channel has the Incoming Webhook connector enabled.

---

## Documentation

| Document | Description |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Deployment topology, pipeline detail, component contracts, security |
| [docs/SETUP.md](docs/SETUP.md) | Step-by-step environment setup and integration configuration |

---

## License

Internal HPE use. Not for external distribution.