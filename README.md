# AI Assistant for Jira Comment Replies

**Intern Project 2026** — Nagasai & Yousef

An AI assistant that generates context-aware draft replies to developer comments on Jira defects, using keyword heuristics with optional Copilot SDK.

---

## MVP v1 — Feature Roadmap

### Phase 1: Architecture & Scaffolding
- [x] Project structure, config, `.env` template
- [x] Core data models — Comment, Classification, Context, Draft, Webhook
- [x] Jira integration — full REST API client (`JiraClient`)
- [x] Webhook receiver — `POST /webhook/jira` accepts Jira comment events
- [x] Event filtering — gates on issue type (Bug/Defect), status, trigger keywords, idempotency
- [x] Tests — Jira client & webhook filter

### Phase 2: Comment Classification
- [x] Comment classification — 4 buckets via keyword heuristics + optional Copilot SDK
- [x] Tests — classifier unit tests

### Phase 3: Context Collection & Draft Generation
- [x] Context retrieval — Jira issue fields, last N comments, attachments, linked issues, changelog
- [x] Jenkins link detection — extracts console-log URLs from descriptions & comments
- [x] Draft generation — template-per-bucket with context substitution + optional Copilot SDK polish
- [x] Evidence & citations — attachments and Jenkins logs tracked per draft
- [x] Suggested labels & actions — auto-suggested per classification type
- [x] Tests — context collector & drafter unit tests

### Phase 4: Full Pipeline & Approval Workflow
- [x] Full pipeline orchestration — webhook → filter → classify → context → draft → store
- [x] Draft store & API — `GET /drafts`, `GET /drafts/{id}`, filter by issue key
- [x] Approval workflow — `POST /approve`, `POST /reject` with feedback
- [x] Tests — end-to-end pipeline tests via FastAPI TestClient

### 🔲 Phase 5: Notifications (`phase/step-5`)
- [ ] Notifications — optional Teams webhook cards + Email (SMTP) on draft generated / approved / rejected
- [ ] Tests — notification unit tests
- [ ] **Current:** 98 tests passed, 93% code coverage (notifications pending)

## Architecture

```
Jira Cloud (Webhook)
    │
    ▼
POST /webhook/jira  ──▶  EventFilter (type, status, keywords, idempotency)
    │
    ▼
CommentClassifier  ──▶  Keyword rules │ Copilot SDK fallback
    │
    ▼
ContextCollector   ──▶  Issue fields, comments, attachments, changelog, Jenkins links
    │
    ▼
ResponseDrafter    ──▶  Template fill + optional Copilot SDK polish
    │
    ▼
Draft Store        ──▶  GET /drafts  │  POST /approve  │  POST /reject
    │
    ▼
Notifications      ──▶  Teams Webhook (card)  │  Email (SMTP)
```

## Classification Buckets (MVP v1)

| Bucket | Trigger keywords |
|---|---|
| Cannot Repro | "cannot reproduce", "can't repro", "works on my machine" |
| Need Info / Logs | "need logs", "provide logs", "need more info" |
| Fixed — Validate | "fix ready", "fix deployed", "please validate", "already fixed" |
| By Design | "as designed", "by design", "expected behavior" |
| Other | (fallback) |

## Project Structure

```
├── src/
│   ├── config.py                 # Centralised settings (env vars)
│   ├── agent/
│   │   ├── classifier.py         # Comment classification (keywords + Copilot SDK)
│   │   ├── context_collector.py  # Jira issue context gathering
│   │   └── drafter.py            # Template-based draft generation
│   ├── api/
│   │   ├── app.py                # FastAPI webhook & approval endpoints
│   │   └── event_filter.py       # Webhook event gate rules
│   ├── integrations/
│   │   ├── jira.py               # Jira Cloud REST API client
│   │   └── __init__.py
│   ├── storage/
│   │   └── sqlite_store.py       # Persistent drafts + event idempotency
│   └── models/
│       ├── classification.py     # CommentType enum + classification model
│       ├── comment.py            # Comment data model
│       ├── context.py            # IssueContext + collection result
│       ├── draft.py              # Draft + DraftStatus models
│       └── webhook.py            # JiraWebhookEvent payload model
├── tests/
│   ├── conftest.py               # Shared fixtures
│   └── unit/
│       ├── test_classifier.py
│       ├── test_context_collector.py
│       ├── test_drafter.py
│       ├── test_jira_client.py
│       ├── test_pipeline.py      # End-to-end via FastAPI TestClient
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

## Security & Production Settings

The API now supports webhook authenticity verification and approval endpoint protection.

Notifications are planned for Phase 5 and are not yet implemented in `src/integrations`.

### Required in production

- `WEBHOOK_SECRET` — shared HMAC secret used to validate incoming webhook signatures
- `APPROVAL_API_KEY` — shared token required on `/approve` and `/reject`
- `ASSISTANT_DB_PATH` — SQLite path for persistent drafts and processed-event idempotency

### Webhook signature header

When `WEBHOOK_SECRET` is set, send one of:

- `X-Hub-Signature-256: sha256=<hex-digest>`
- `X-Webhook-Signature: <hex-digest>`

Digest is `HMAC_SHA256(secret, raw_request_body)`.

### Approval auth header

When `APPROVAL_API_KEY` is set, include:

- `X-Approval-Token: <APPROVAL_API_KEY>`

### Example: signed webhook request

```bash
payload='{"webhookEvent":"comment_created","timestamp":1700000001,"issue":{"id":"1","key":"DEFECT-500","fields":{"summary":"Test issue","issuetype":{"name":"Bug"},"status":{"name":"Open"}}},"comment":{"id":"99001","body":"Cannot reproduce this on my machine.","author":{"accountId":"u1","displayName":"Dev User","emailAddress":"dev@company.com"},"created":"2025-02-23T10:30:00.000+0000","updated":"2025-02-23T10:30:00.000+0000"}}'
sig=$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')
curl -X POST http://localhost:8000/webhook/jira \
    -H 'Content-Type: application/json' \
    -H "X-Hub-Signature-256: sha256=$sig" \
    -d "$payload"
```

### Example: approve with token

```bash
curl -X POST http://localhost:8000/approve \
    -H 'Content-Type: application/json' \
    -H "X-Approval-Token: $APPROVAL_API_KEY" \
    -d '{"draft_id":"draft_1700000000","approved_by":"qa@company.com"}'
```

### Approve response fields

`POST /approve` now returns posting metadata so integrators can distinguish
approval-only from successfully-posted outcomes.

- `status` — approval API result (`approved`)
- `draft_id` — draft identifier
- `posted_to_jira` — `true` when comment was posted to Jira, `false` otherwise
- `jira_comment_id` — Jira comment id when posting succeeds, else `null`
- `post_reason` — failure/skip reason when not posted, else `null`

Example response:

```json
{
    "status": "approved",
    "draft_id": "draft_1700000000",
    "posted_to_jira": true,
    "jira_comment_id": "123456",
    "post_reason": null
}
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — Component design and data flow
- [Setup Guide](docs/SETUP.md) — Installation, configuration, and testing
