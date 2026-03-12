# Architecture Overview

## High-Level Flow

```
1. Jira Webhook Event (comment_created / comment_updated)
   └─▶ FastAPI Webhook Receiver

2. Event Filtering
   ├─▶ Event type gate (comment_created, comment_updated, jira:issue_updated)
   ├─▶ Idempotency check (dedup by event ID)
   ├─▶ Issue type gate (Bug / Defect only)
   ├─▶ Status gate (Open, In Progress, Ready for QA, Reopened, To Do, In Review)
   └─▶ Keyword heuristic gate (trigger keywords in comment body)

3. Classification
   ├─▶ Copilot SDK structured classification (if API key configured)
   └─▶ Keyword fallback (always available)
   └─▶ 8 buckets: Cannot Repro | Need Info | Fixed Validate | By Design
                   | Duplicate/Fixed | Blocked/Waiting | Config Issue | Other

4. Context Collection
   ├─▶ Issue fields (summary, description, environment, versions, components)
   ├─▶ Last N comments (default 10)
   ├─▶ Attachment metadata
   ├─▶ Linked issues
   ├─▶ Changelog (status transitions)
   ├─▶ Jenkins console-log URL detection
   ├─▶ RAG snippets (Confluence + PDFs)         
   ├─▶ Log entries (Jenkins / ELK / file)        
   ├─▶ TestRail results                          
   └─▶ Build pipeline metadata                   

5. Draft Generation
   ├─▶ Template selection (by classification bucket — 8 templates)
   ├─▶ Template variable substitution (from context)
   ├─▶ Optional Copilot SDK / local LLM refinement
   ├─▶ Citation extraction + evidence tracking
   └─▶ Suggested labels + actions

6. Storage & Approval
   ├─▶ SQLite persistent draft store
   ├─▶ GET /drafts, GET /drafts/{id}
   ├─▶ POST /approve → marks draft approved
   └─▶ POST /reject → marks draft rejected with feedback

7. Notifications (optional)
   ├─▶ Teams webhook → MessageCard per event (generated / approved / rejected)
   └─▶ Email (SMTP) → HTML summary per event
```

## Pipeline Stages

1. **Webhook ingest**
   - Receives Jira comment events.
   - Validates payload structure.
   - Optionally validates HMAC signature when `WEBHOOK_SECRET` is configured.

2. **Event filtering**
   - Checks event type.
   - Enforces idempotency.
   - Applies issue-type/status/keyword gates.

3. **Classification**
   - Classifies comment intent into MVP buckets.
   - Uses keyword logic with optional LLM-assisted refinement.

4. **Context collection**
   - Fetches Jira issue details, comments, links, attachments, changelog.
   - Extracts Jenkins console log URLs where present.

5. **Draft generation**
   - Selects template by classification.
   - Fills template from context.
   - Produces citations and suggested actions/labels.

6. **Persistence and decisions**
   - Stores drafts and processed events in SQLite.
   - Supports reviewer actions: approve or reject.
   - On approve, attempts to post draft back to Jira.

7. **Notifications (optional)**
   - Sends generated/approved/rejected events to configured channels.

## Runtime Components

### API Layer — [../src/api/app.py](../src/api/app.py)
- FastAPI application and orchestration entrypoint
- Endpoints:
  - `POST /webhook/jira` — Receive and process comment events
  - `GET /health` — Health check
  - `GET /drafts` — List all drafts (filter by `?issue_key=`)
  - `GET /drafts/{draft_id}` — Retrieve a specific draft
  - `POST /approve` — Approve a draft
  - `POST /reject` — Reject a draft with feedback
  - `POST /rag/ingest/pdf` — Upload and ingest a PDF
  - `POST /rag/ingest/text` — Ingest raw text
  - `POST /rag/ingest/confluence` — Sync Confluence pages
  - `GET /rag/search` — Semantic search over indexed docs
  - `GET /rag/stats` — RAG collection statistics
  - `DELETE /rag/document/{source_title}` — Remove an indexed document

### 2. Event Filter (`src/api/event_filter.py`)
- Stateful filter with in-memory idempotency set
- Six gate rules applied in sequence
- Returns `FilterResult(accepted, reason, event_id)`
- Trigger keywords cover all 8 classification buckets

### 3. Comment Classifier (`src/agent/classifier.py`)
- Two-tier classification: Copilot SDK → keyword fallback
- 8 classification buckets:
  - `cannot_reproduce` — Developer cannot reproduce the issue
  - `need_more_info` — Requesting logs, environment details, or other info
  - `fixed_validate` — Fix ready, needs validation
  - `by_design` — Behavior is by design / expected
  - `duplicate_fixed` — Duplicate or already fixed in another ticket
  - `blocked_waiting` — Blocked by dependency or waiting for something
  - `config_issue` — Configuration / setup issue, not a code defect
  - `other` — Fallback
- Returns `CommentClassification` with confidence, reasoning, missing context, suggested questions

### 4. Context Collector (`src/agent/context_collector.py`)
- Calls `JiraClient` to gather full issue context
- Builds `IssueContext` with fields, comments, attachments, links, changelog
- Detects Jenkins console-log URLs
- Queries RAG engine for relevant document snippets (when configured)
- Returns `ContextCollectionResult` with timing metrics, RAG snippets, and evidence pointers

### 5. Response Drafter (`src/agent/drafter.py`)
- One template per classification bucket (8 templates)
- Safe `format_map` substitution with context-derived values
- Helpers: `_find_related_ticket`, `_find_blocking_item` for linked issue references
- Includes RAG snippets in evidence formatting, citations, and `evidence_used` tracking
- Optional Copilot SDK refinement for natural language polish
- Generates citations, evidence tracking, suggested labels, and suggested actions

### 6. Jira Client (`src/integrations/jira.py`)
- Wraps `atlassian-python-api` for Jira Cloud REST API
- Read: get_issue, get_comments, get_last_comments, get_attachments, get_linked_issues, get_changelog, detect_jenkins_links
- Write: add_comment, update_custom_field, add_label, transition_issue

### 7. Notification Service (`src/integrations/notifications.py`)
- **TeamsNotifier** — Posts MessageCard JSON to an incoming webhook URL
- **EmailNotifier** — Sends HTML email via SMTP (TLS, optional auth)
- **NotificationService** — Facade that fans out to both channels
- Fires on: draft generated, draft approved, draft rejected
- Both channels are optional — silently skipped when env vars are empty

### 8. SQLite Draft Store (`src/storage/sqlite_store.py`)
- Persistent draft storage replacing in-memory dict
- CRUD: `save`, `get`, `list_all`, `count`, `update_status`, `mark_posted`, `delete`
- Indexed on `issue_key`, `status`, `created_at`
- WAL journal mode for concurrent reads
- Full Draft JSON stored alongside indexed columns

### 9. RAG Engine (`src/rag/engine.py`)
- ChromaDB-backed vector store for semantic document retrieval
- Sentence-transformer embeddings (default: `all-MiniLM-L6-v2`)
- `add_chunks` — upsert document chunks into the collection
- `query` — semantic search with optional source_type filter, returns ranked `RAGResult`
- `delete_by_source` / `delete_by_id` — remove indexed documents
- `stats` — collection size, source distribution, config info
- Cosine similarity scoring (distance → relevance conversion)

### 10. Document Ingester (`src/rag/ingest.py`)
- Sliding-window text chunking with configurable size and overlap
- Prefers paragraph and sentence boundary breaks when splitting
- `ingest_pdf` — parses PDF via pypdf, chunks, and indexes
- `ingest_text` — chunks raw text and indexes with metadata
- `ingest_confluence_page` — fetches Confluence page content, chunks, and indexes

### 11. Confluence Client (`src/integrations/confluence.py`)
- Wraps `atlassian-python-api` Confluence client
- `get_page` — fetch page by ID with storage body
- `get_page_content_as_text` — strip HTML to plain text for chunking
- `search_pages` — CQL search by space and/or label
- HTML-to-text conversion with entity decoding, script removal, whitespace normalisation

## Data Models (`src/models/`)

| Model | Purpose |
|---|---|
| `JiraWebhookEvent` | Incoming webhook payload with derived helpers |
| `Comment` | Normalised Jira comment |
| `CommentClassification` | Classification result with confidence (8 buckets) |
| `IssueContext` | Full issue context snapshot |
| `ContextCollectionResult` | Context + Jenkins links + RAG snippets + log entries + timing |
| `Draft` | Generated response with citations, evidence tracking, and approval state |
| `RAGSnippet` | Single retrieval result from RAG index |
| `RAGResult` | Aggregated RAG retrieval result |
| `LogEntry` | Log entry from Jenkins / ELK / file lookup |
| `DocumentChunk` | Document chunk stored in vector index |

## Configuration (`src/config.py`)

| Config Class | Purpose |
|---|---|
| `JiraConfig` | Jira Cloud credentials |
| `CopilotConfig` | Copilot SDK / OpenAI API settings |
| `LLMConfig` | Local LLM (llama.cpp / GGUF) settings |
| `RAGConfig` | ChromaDB, embedding model, chunking settings |
| `ConfluenceConfig` | Confluence API credentials for RAG ingestion |
| `TestRailConfig` | TestRail API credentials |
| `LogLookupConfig` | Jenkins / log directory settings |
| `NotificationConfig` | Teams + Email / SMTP settings |
| `AppConfig` | Host, port, log level, DB path |
