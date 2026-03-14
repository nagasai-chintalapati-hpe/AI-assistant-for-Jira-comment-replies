# Architecture Overview

## High-Level Flow

```
1. Jira Webhook Event (comment_created / comment_updated)
   └─▶ FastAPI Webhook Receiver  (POST /webhook/jira)

2. Event Filtering  (src/api/event_filter.py)
   ├─▶ Event type gate        — comment_created, comment_updated only
   ├─▶ Idempotency check      — dedup by deterministic event ID
   ├─▶ Issue type gate        — Bug / Defect only
   └─▶ Status gate            — Open, In Progress, Ready for QA, Reopened,
                                To Do, In Review, Cannot Reproduce,
                                Closed, Resolved, Done, …

3. Classification  (src/agent/classifier.py)
   ├─▶ Local LLM / Copilot SDK  (if configured)
   └─▶ Keyword fallback         (always available)
   Buckets: cannot_reproduce | need_more_info | fixed_validate | by_design
            | duplicate_fixed | blocked_waiting | config_issue | other

4. Context Collection  (src/agent/context_collector.py)
   ├─▶ Jira API        — issue fields, comments, attachments, links, changelog
   ├─▶ TestRail        — run summaries detected from R<id> references in issue text
   ├─▶ Git             — PR metadata detected from PR URLs in issue text
   ├─▶ RAG engine      — semantic search over indexed Confluence / PDFs
   ├─▶ Log lookup      — Jenkins console logs / local log files
   ├─▶ ELK             — OpenSearch / Elasticsearch log queries
   └─▶ S3              — build artifact fetch by detected build ID

5. Draft Generation  (src/agent/drafter.py)
   ├─▶ Template selection  (one per bucket, 8 total)
   ├─▶ Variable substitution from collected context
   ├─▶ Optional LLM refinement
   ├─▶ Hallucination detection
   └─▶ Citation + evidence tracking, suggested labels + actions

6. Storage & Human-in-the-Loop Approval
   ├─▶ SQLite draft store (persistent)
   ├─▶ Review UI  GET /ui  — list, filter, view drafts
   ├─▶ POST /approve  → posts approved comment back to Jira
   └─▶ POST /reject   → stores feedback

7. Notifications 
   ├─▶ Teams  — AdaptiveCard via incoming webhook
   └─▶ Email  — HTML summary via SMTP
```

## Components

### Webhook Receiver (`src/api/app.py`)
FastAPI server. Parses Jira payloads, runs the full pipeline, serves the Review UI.

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/jira` | Receive Jira comment events |
| `GET` | `/health` | Health + integration status |
| `GET` | `/ui` | Draft Review UI (list) |
| `GET` | `/ui/drafts/{id}` | Draft detail + approve/reject |
| `GET/POST` | `/drafts`, `/drafts/{id}` | JSON API for drafts |
| `POST` | `/approve`, `/reject` | Programmatic approve/reject |
| `POST` | `/rag/ingest/*` | Ingest PDF / text / Confluence |
| `GET` | `/rag/search`, `/rag/stats` | RAG query + stats |

### Event Filter (`src/api/event_filter.py`)
Five gates: event type → idempotency → issue type → status → issue/comment presence.
Keyword matching is informational only — used by the classifier, not as a gate.

### Comment Classifier (`src/agent/classifier.py`)
Two-tier: LLM → keyword fallback. Returns `CommentClassification` with confidence, reasoning, missing context, and suggested questions.

### Context Collector (`src/agent/context_collector.py`)
Orchestrates all external API calls. Returns `ContextCollectionResult` with issue context, TestRail results, Git PRs, RAG snippets, log entries, and collection timing.
- TestRail: detects `R<id>` / `run/<id>` patterns in issue text/comments
- Git PRs: detects PR URLs and branch names containing the issue key

### Response Drafter (`src/agent/drafter.py`)
Template-based generation with optional LLM refinement. Includes hallucination detection to flag low-confidence substitutions.

### Integrations

| Module | Purpose |
|---|---|
| `jira.py` | Jira Cloud REST — read issue, comments, attachments; write comments |
| `testrail.py` | TestRail API v2 — runs, tests, results (API key or session cookie auth) |
| `git.py` | GitHub / GitLab / Bitbucket — PR metadata |
| `confluence.py` | Confluence Cloud — page fetch + CQL search for RAG ingestion |
| `log_lookup.py` | Jenkins console logs + local log file scanning |
| `notifications.py` | Teams AdaptiveCard + Email (SMTP) |
| `s3_connector.py` | S3 / MinIO artifact fetch by build ID |

### Storage (`src/storage/sqlite_store.py`)
SQLite with WAL mode. Stores full draft JSON + indexed columns for fast filtering.

### RAG Pipeline (`src/rag/`)
ChromaDB vector store with sentence-transformer embeddings. Ingests PDF, plain text, and Confluence pages. Returns ranked snippets for context enrichment.

### Queue & Rate Limiting
- `src/queue/broker.py` — RabbitMQ for async webhook processing (optional, set `QUEUE_ENABLED=true`)
- Rate limiter in `src/api/app.py` — token bucket, Redis-backed in HA deployments

## Data Models (`src/models/`)

| Model | Purpose |
|---|---|
| `JiraWebhookEvent` | Incoming webhook payload |
| `Comment` | Normalised Jira comment |
| `CommentClassification` | Classification result (8 buckets + confidence) |
| `IssueContext` | Full issue context snapshot |
| `ContextCollectionResult` | Context + all integration results + timing |
| `Draft` | Generated reply with citations, evidence, approval state |

## Configuration (`src/config.py`)

All settings read from environment variables (`.env` file). See `.env.example` for the full reference.

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
