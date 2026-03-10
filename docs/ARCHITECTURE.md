# Architecture Overview

## High-Level Flow

```
Jira Cloud (Webhook)
    │
    ▼
POST /webhook/jira  ──▶  EventFilter
    │
    ▼
CommentClassifier
    │
    ▼
ContextCollector
    │
    ▼
ResponseDrafter
    │
    ▼
SQLiteStore (drafts + idempotency)
    │
    ├─▶ GET /drafts
    ├─▶ GET /drafts/{draft_id}
    ├─▶ POST /approve
    └─▶ POST /reject
    │
    ▼
Optional Notifications (Teams / Email)
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
  - `GET /health`
  - `POST /webhook/jira`
  - `GET /drafts`
  - `GET /drafts/{draft_id}`
  - `POST /approve`
  - `POST /reject`

### Event Filter — [../src/api/event_filter.py](../src/api/event_filter.py)
- Gatekeeper for processable Jira comment events
- Returns acceptance/rejection reason and event identity

### Classifier — [../src/agent/classifier.py](../src/agent/classifier.py)
- Maps comment text to intent bucket
- Provides confidence and rationale metadata

### Context Collector — [../src/agent/context_collector.py](../src/agent/context_collector.py)
- Builds normalized issue context for drafting
- Adds evidence references (attachments, links, Jenkins logs)

### Drafter — [../src/agent/drafter.py](../src/agent/drafter.py)
- Generates reviewer-ready reply drafts
- Maintains deterministic template output with optional language polish

### Jira Integration — [../src/integrations/jira.py](../src/integrations/jira.py)
- Jira REST read/write adapter
- Used for context retrieval and posting approved comments

### Notifications — [../src/integrations/notifications.py](../src/integrations/notifications.py)
- Teams and SMTP email notifiers
- Fan-out service used by pipeline/approval paths

### Persistence — [../src/storage/sqlite_store.py](../src/storage/sqlite_store.py)
- SQLite-backed draft persistence
- Processed-event tracking for idempotency

## Security Model

- **Webhook authenticity:** HMAC-SHA256 signature via `WEBHOOK_SECRET`
- **Approval authorization:** shared token via `X-Approval-Token` and `APPROVAL_API_KEY`
- **Production enforcement:** set `ENV=production` to enforce stricter required config

## Data Models

| Model | Purpose |
|---|---|
| `JiraWebhookEvent` | Incoming webhook payload and helpers |
| `Comment` | Normalized Jira comment |
| `CommentClassification` | Bucket + confidence metadata |
| `IssueContext` | Collected issue context |
| `ContextCollectionResult` | Context and collection metadata |
| `Draft` | Generated draft, status, and review metadata |
