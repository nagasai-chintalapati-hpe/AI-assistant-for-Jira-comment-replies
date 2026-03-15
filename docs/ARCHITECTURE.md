# Architecture Overview

## Deployment Topology

```
┌─────────────────────────────────────────────────────────────────┐
│  Internet / SaaS                                                │
│  ┌─────────────┐                                                │
│  │  Jira Cloud │  ◀── Outbound HTTPS / Jira REST API (read)    │
│  └──────┬──────┘                                                │
│         │  Webhook: comment_created                             │
└─────────┼───────────────────────────────────────────────────────┘
          │
┌─────────▼───────────────────────────────────────────────────────┐
│  DMZ / Perimeter Zone                                           │
│  ┌─────────────────────────────────┐                            │
│  │  Webhook Relay                  │                            │
│  │  Validates signature            │                            │
│  │  Enqueues event                 │                            │
│  └──────────────┬──────────────────┘                            │
│                 │                                               │
│  ┌──────────────▼──────────────────┐                            │
│  │  Queue (RabbitMQ / Kafka)        │                            │
│  └──────────────┬──────────────────┘                            │
└─────────────────┼───────────────────────────────────────────────┘
                  │  Consume events
┌─────────────────▼───────────────────────────────────────────────┐
│  On-Prem Network                                                │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Agent Service                                           │   │
│  │                                                          │   │
│  │  ┌─────────────┐   ┌──────────────┐                     │   │
│  │  │     LLM     │   │ RAG Retriever│──▶ RAG Pipeline     │   │
│  │  │(llama.cpp / │   │  (ChromaDB)  │                     │   │
│  │  │ Copilot SDK)│   └──────────────┘                     │   │
│  │  └─────────────┘                                         │   │
│  │  ┌─────────────┐   ┌──────────────┐                     │   │
│  │  │ Classifier  │   │   Drafter    │──▶ Review UI        │   │
│  │  └─────────────┘   └──────────────┘                     │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────┐  ┌─────────────┐  ┌─────────────────────┐   │
│  │ Local LLM    │  │ Vector Store│  │ Confluence/PDF       │   │
│  │ Server       │  │ (ChromaDB)  │  │ Indexer + Store      │   │
│  └──────────────┘  └─────────────┘  └─────────────────────┘   │
│                                                                 │
│  ┌──────────────┐  ┌─────────────┐  ┌─────────────────────┐   │
│  │  TestRail    │  │    Logs     │  │  S3 / Build Artifact │   │
│  │  Connector   │  │  Connector  │  │  Store               │   │
│  └──────────────┘  └─────────────┘  └─────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## End-to-End Pipeline

```
1. Jira Webhook Event (comment_created)
   └─▶ Webhook Relay validates HMAC signature → enqueues to RabbitMQ/Kafka

2. Agent Service consumes event
   └─▶ Event Filter (src/api/event_filter.py)
       ├─▶ Event type    — comment_created / comment_updated only
       ├─▶ Idempotency   — dedup by deterministic event ID
       ├─▶ Issue type    — Bug / Defect only
       └─▶ Status gate   — Open, In Progress, Ready for QA, Reopened, …

3. Classification  (src/agent/classifier.py)
   ├─▶ LLM / Copilot SDK  (if configured)
   └─▶ Keyword fallback   (always available)
   Buckets: cannot_reproduce | need_more_info | fixed_validate | by_design
            | duplicate_fixed | blocked_waiting | config_issue | other

4. Context Collection  (src/agent/context_collector.py)
   ├─▶ Jira         — issue fields, comments, attachments, links, changelog
   ├─▶ TestRail     — run summaries from R<id> markers in issue text
   ├─▶ Git          — PR metadata from PR URLs / branch names
   ├─▶ RAG          — semantic search over Confluence + PDFs (top 5 snippets)
   ├─▶ Log lookup   — Jenkins console logs / local log files
   ├─▶ ELK          — OpenSearch log queries by build/run ID + time window
   └─▶ S3           — build artifact fetch by detected build ID

5. Duplicate Detection  (src/agent/duplicate_detector.py)
   ├─▶ Fetches past drafts on the same issue_key via find_recent_by_issue() (180-day window)
   ├─▶ Jaccard token similarity (threshold 0.25) between incoming comment and each past draft body
   └─▶ Top-N similar drafts attached to Draft.similar_drafts; surfaced as warning in review UI

5b. Pattern Detection  (src/api/orchestrator.py → _detect_pattern)
   ├─▶ Extracts component[0] + affectedVersion[0] from collected issue context
   ├─▶ JQL: issuetype in (Bug, Defect) AND status not in (Done, Closed, Resolved) AND component=X AND affectedVersion=Y
   └─▶ If count ≥ 3, attaches pattern note to Draft.pattern_note; shown as red alert in review UI

6. Draft Generation  (src/agent/drafter.py)
   ├─▶ Template per bucket (8 total)
   │   Structure: ✅ Acknowledge · 🔎 Evidence · 🧪 Repro steps
   │              ❓ Missing info · ▶️ Next action
   ├─▶ Evidence citations (TestRail / PR / Confluence / log excerpts)
   ├─▶ Duplicate warning and pattern note forwarded from steps 5/5b
   ├─▶ Optional LLM refinement + hallucination detection
   └─▶ Suggested labels + transitions

7. Human-in-the-Loop Approval  (no auto-post policy)
   ├─▶ SQLite draft store — full audit trail
   ├─▶ Review UI  GET /ui — list · filter · view · edit drafts
   ├─▶ Teams AdaptiveCard — draft text + evidence + approve/reject actions
   ├─▶ POST /approve → posts approved comment back to Jira
   └─▶ POST /reject  → stores feedback

8. Audit & Observability
   ├─▶ Every draft stores: event ID, issue key, comment ID, inputs used,
   │   evidence links, draft text, classification, confidence, who approved
   └─▶ Redaction stats, hallucination flags, time-saved metrics
```

## Components

### Agent Service (`src/api/app.py`, `src/agent/`)
Hosted on-prem. Receives events from the queue, orchestrates the full pipeline (classify → collect → duplicate check → pattern detect → draft), enforces policies (redaction, grounding, audit), and serves the Review UI.

**`DuplicateDetector` (`src/agent/duplicate_detector.py`)** — Jaccard token similarity check against past drafts stored in SQLite. Threshold 0.25. Returns a `DuplicateCheckResult` with ranked `SimilarDraft` entries surfaced in the review UI as a warning card.

**`_detect_pattern` (`src/api/orchestrator.py`)** — Live JQL query to find 3+ open Bug/Defect issues sharing the same component and affected version. Attaches a plain-text pattern note to the draft, shown as a red alert in the review UI.

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/jira` | Direct webhook receive (dev / single-node) |
| `GET` | `/health` | Health + integration status |
| `GET` | `/ui` | Draft Review UI |
| `GET/POST` | `/drafts`, `/drafts/{id}` | JSON draft API |
| `POST` | `/approve`, `/reject` | Approve / reject with feedback |
| `POST` | `/rag/ingest/*` | Ingest PDF / text / Confluence |
| `GET` | `/rag/search` | RAG semantic query |

### Connectors / Tools

| Module | Spec Name | Purpose |
|---|---|---|
| `jira.py` | JiraConnector | Issue fields, comments, attachments, write comments |
| `testrail.py` | TestRailConnector | Run/case results by R\<id\> marker (API key or session cookie) |
| `git.py` | — | GitHub / GitLab / Bitbucket PR metadata |
| `confluence.py` | Confluence/PDFConnector | Page fetch + CQL search for RAG ingestion |
| `log_lookup.py` | LogStoreConnector | Jenkins console logs + ELK queries by build/time window |
| `s3_connector.py` | S3ArtifactFetcher | Pre-signed URL artifact fetch by build ID |
| `notifications.py` | — | Teams AdaptiveCard + Email (SMTP) |

### RAG Index (`src/rag/`)
ChromaDB vector store with sentence-transformer embeddings. Ingests Confluence pages, PDFs, runbooks, and known-issues docs. Stores chunk metadata (component/version/env) and returns citations per snippet.

### Storage (`src/storage/sqlite_store.py`)
SQLite with WAL mode. Stores full draft JSON, inputs used, evidence links, approval state, and redaction stats for every event. `find_recent_by_issue(issue_key, limit, days)` provides a 180-day rolling window of past drafts used by the duplicate detector.

### Queue & Rate Limiting
- `src/queue/broker.py` — RabbitMQ/Kafka async event processing (`QUEUE_ENABLED=true`)
- Rate limiter in `src/api/app.py` — token bucket, Redis-backed for HA deployments

### Security & Redaction
- `src/utils/redactor.py` — strips secrets, PII, and internal tokens before LLM calls
- Webhook HMAC signature validation (`VALIDATE_WEBHOOK_SIGNATURE=true`)
- Least-privilege Jira scopes: read issues + post comments only

## Data Models (`src/models/`)

| Model | Purpose |
|---|---|
| `JiraWebhookEvent` | Incoming webhook payload |
| `Comment` | Normalised Jira comment |
| `CommentClassification` | Classification result (8 buckets + confidence) |
| `IssueContext` | Full issue context snapshot |
| `ContextCollectionResult` | Context + all integration results + timing |
| `Draft` | Generated reply with citations, evidence, approval state, `similar_drafts`, and `pattern_note` |

## Configuration (`src/config.py`)

All settings from environment variables. See `.env.example` for the full reference.

| Config Class | Key Variables |
|---|---|
| `JiraConfig` | `JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` |
| `LLMConfig` | `LLM_BACKEND`, `LLM_MODEL_PATH` |
| `TestRailConfig` | `TESTRAIL_BASE_URL`, `TESTRAIL_SESSION_COOKIE` |
| `GitConfig` | `GIT_TOKEN`, `GIT_OWNER`, `GIT_REPO` |
| `RAGConfig` | `CHROMA_PERSIST_DIR`, `RAG_EMBEDDING_MODEL` |
| `NotificationConfig` | `TEAMS_WEBHOOK_URL`, `SMTP_HOST`, `EMAIL_TO` |
| `WebhookConfig` | `JIRA_WEBHOOK_SECRET`, `VALIDATE_WEBHOOK_SIGNATURE` |
| `RateLimitConfig` | `RATE_LIMIT_ENABLED`, `RATE_LIMIT_RPM` |
| `S3Config` | `S3_BUCKET`, `S3_REGION`, `AWS_ACCESS_KEY_ID` |
| `QueueConfig` | `QUEUE_ENABLED`, `QUEUE_URL`, `QUEUE_NAME` |

## Objective

Build an on-prem agentic AI assistant that helps QA teams respond to developer comments on Jira defects by generating context-aware, evidence-grounded draft replies — with human approval before any comment is posted back to Jira.

**Key principles:**
- **Human-in-the-loop** — no auto-post, ever. Drafts route for approval first.
- **Grounded in evidence** — every claim backed by a citation (TestRail / Confluence / log / PR).
- **On-prem by default** — LLM, RAG, and secrets never leave the HPE network.
- **Measurable** — track draft acceptance rate, edits made, time saved, hallucination rate.
