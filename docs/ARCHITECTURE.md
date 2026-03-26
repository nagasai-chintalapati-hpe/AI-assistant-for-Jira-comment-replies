# Architecture

## Deployment

```
┌──────────────────────────────────────────────────────────────┐
│  SaaS                                                        │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │ Jira Cloud │  │ GitHub / │  │ TestRail │  │ MS Teams  │ │
│  │            │  │ GitLab   │  │          │  │           │ │
│  └─────┬──────┘  └──────────┘  └──────────┘  └───────────┘ │
│        │ webhook: comment_created                            │
└────────┼─────────────────────────────────────────────────────┘
         │
┌────────▼─────────────────────────────────────────────────────┐
│  DMZ                                                         │
│  Webhook Relay (ngrok / nginx) + HMAC validation             │
│  RabbitMQ (optional async queue)                             │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│  On-Prem / Docker                                            │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  FastAPI Agent Service (:8000)                         │  │
│  │                                                        │  │
│  │  Event Filter → Classifier → Context Collector         │  │
│  │       → Duplicate Detector → Pattern Detector          │  │
│  │       → Drafter → Review UI / Teams Notification       │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐               │
│  │  SQLite    │ │  ChromaDB  │ │  Redis     │               │
│  │  (drafts)  │ │  (RAG)     │ │  (optional)│               │
│  └────────────┘ └────────────┘ └────────────┘               │
└──────────────────────────────────────────────────────────────┘
```

## Pipeline

| # | Stage | Module | Description |
|---|-------|--------|-------------|
| 1 | Webhook | `event_filter.py` | HMAC validation, dedup, bot guard, issue-type gate |
| 2 | Classify | `classifier.py` | LLM or keyword rules → 8 buckets + confidence 0–1 |
| 3 | Collect | `context_collector.py` | Parallel fan-out: Jira, TestRail, Git, Jenkins, ELK, S3, RAG |
| 4 | Duplicates | `duplicate_detector.py` | Jaccard similarity vs past drafts (180-day window) |
| 5 | Patterns | `orchestrator.py` | JQL for 3+ open bugs on same component + version |
| 6 | Draft | `drafter.py` | Template + evidence + optional LLM refinement |
| 7 | Review | `/ui`, Teams card | Human approves, edits, or rejects |
| 8 | Post | `/approve` | Writes audit field + posts comment to Jira |

## Connectors

| Module | Purpose |
|--------|---------|
| `jira.py` | Issue fields, comments, attachments, write comments |
| `testrail.py` | Test run/case results by R‹id› marker |
| `git.py` | GitHub / GitLab / Bitbucket PR metadata (multi-repo) |
| `confluence.py` | Page fetch + CQL search for RAG ingestion |
| `jenkins.py` | Build info, console logs, failure analysis |
| `log_lookup.py` | Jenkins console + ELK log queries |
| `s3_connector.py` | Pre-signed URL artifact fetch by build ID |
| `notifications.py` | Teams AdaptiveCard + Email (SMTP) |

## RAG

ChromaDB with sentence-transformer embeddings. Dual-query at runtime — KB (Confluence/PDFs) + prior-defect (resolved Jira tickets). Results deduplicated by `chunk_id` before injection into the draft prompt.

## Storage

SQLite (WAL mode). Stores draft JSON, classification, evidence, approval state, ratings, redaction stats. Powers the Dashboard analytics.

## Security

- HMAC-SHA256 webhook signature validation
- Per-IP rate limiting (Redis-backed for HA)
- PII redaction before LLM calls
- Least-privilege Jira scopes
- Non-root Docker user

## Classification Buckets

`cannot_reproduce` · `need_more_info` · `fixed_validate` · `by_design` · `duplicate_fixed` · `blocked_waiting` · `configuration_issue` · `other`

## Data Models

| Model | Purpose |
|-------|---------|
| `JiraWebhookEvent` | Incoming webhook payload |
| `Comment` | Normalised Jira comment |
| `CommentClassification` | Bucket + confidence |
| `IssueContext` | Full issue context snapshot |
| `Draft` | Reply with citations, evidence, approval state, `similar_drafts`, `pattern_note` |

## Design Principles

- **Human-in-the-loop** — no auto-post; every draft requires approval
- **Evidence-grounded** — every claim backed by a citation
- **On-prem by default** — LLM, RAG, and secrets stay on internal network
- **Graceful degradation** — each integration is optional
