# Architecture Overview — MVP v1

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

4. Context Collection
   ├─▶ Issue fields (summary, description, environment, versions, components)
   ├─▶ Last N comments (default 10)
   ├─▶ Attachment metadata
   ├─▶ Linked issues
   ├─▶ Changelog (status transitions)
   └─▶ Jenkins console-log URL detection

5. Draft Generation
   ├─▶ Template selection (by classification bucket)
   ├─▶ Template variable substitution (from context)
   ├─▶ Optional Copilot SDK natural-language refinement
   ├─▶ Citation extraction
   └─▶ Suggested labels + actions

6. Storage & Approval
   ├─▶ In-memory draft store (MVP v1)
   ├─▶ GET /drafts, GET /drafts/{id}
   ├─▶ POST /approve → marks draft approved
   └─▶ POST /reject → marks draft rejected with feedback

7. Notifications (optional)
   ├─▶ Teams webhook → MessageCard per event (generated / approved / rejected)
   └─▶ Email (SMTP) → HTML summary per event
```

## Components

### 1. Webhook Receiver (`src/api/app.py`)
- FastAPI server listening for Jira webhook events
- Parses payload into `JiraWebhookEvent` (Pydantic model)
- Orchestrates the full pipeline: filter → classify → context → draft → store
- Endpoints:
  - `POST /webhook/jira` — Receive and process comment events
  - `GET /health` — Health check
  - `GET /drafts` — List all drafts (filter by `?issue_key=`)
  - `GET /drafts/{draft_id}` — Retrieve a specific draft
  - `POST /approve` — Approve a draft
  - `POST /reject` — Reject a draft with feedback

### 2. Event Filter (`src/api/event_filter.py`)
- Stateful filter with in-memory idempotency set
- Five gate rules applied in sequence
- Returns `FilterResult(accepted, reason, event_id)`

### 3. Comment Classifier (`src/agent/classifier.py`)
- Two-tier classification: Copilot SDK → keyword fallback
- 4 classification buckets + fallback (see README)
- Returns `CommentClassification` with confidence score, reasoning, missing context, suggested questions

### 4. Context Collector (`src/agent/context_collector.py`)
- Calls `JiraClient` to gather full issue context
- Builds `IssueContext` with fields, comments, attachments, links, changelog
- Detects Jenkins console-log URLs
- Returns `ContextCollectionResult` with timing metrics

### 5. Response Drafter (`src/agent/drafter.py`)
- One template per classification bucket
- Safe `format_map` substitution with context-derived values
- Optional Copilot SDK refinement for natural language polish
- Generates citations, suggested labels, and suggested actions

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

## Data Models (`src/models/`)

| Model | Purpose |
|---|---|
| `JiraWebhookEvent` | Incoming webhook payload with derived helpers |
| `Comment` | Normalised Jira comment |
| `CommentClassification` | Classification result with confidence |
| `IssueContext` | Full issue context snapshot |
| `ContextCollectionResult` | Context + Jenkins links + collection timing |
| `Draft` | Generated response with citations and approval state |
