# AI Assistant for Jira Comment Replies

**Intern Project 2026** — Nagasai & Yousef

An AI assistant that generates context-aware draft replies to developer comments on Jira defects, using keyword heuristics with Copilot SDK.

---

## MVP v1 — Feature Roadmap

### Phase 1: Architecture & Scaffolding (Current)
- [x] Project structure, config, `.env` template
- [x] Core data models — Comment, Classification, Context, Draft, Webhook
- [x] Jira integration — full REST API client (`JiraClient`)
- [x] Webhook receiver — `POST /webhook/jira` accepts Jira comment events
- [x] Event filtering — gates on issue type (Bug/Defect), status, trigger keywords, idempotency
- [x] Tests — Jira client & webhook filter

### 🔲 Phase 2: Comment Classification (`phase/step-2`)
- [ ] Comment classification — 4 buckets via keyword heuristics + optional Copilot SDK
- [ ] Tests — classifier unit tests

### 🔲 Phase 3: Context Collection & Draft Generation (`phase/step-3`)
- [ ] Context retrieval — Jira issue fields, last N comments, attachments, linked issues, changelog
- [ ] Jenkins link detection — extracts console-log URLs from descriptions & comments
- [ ] Draft generation — template-per-bucket with context substitution + optional Copilot SDK polish
- [ ] Evidence & citations — attachments and Jenkins logs tracked per draft
- [ ] Suggested labels & actions — auto-suggested per classification type
- [ ] Tests — context collector & drafter unit tests

### 🔲 Phase 4: Full Pipeline & Approval Workflow (`phase/step-4`)
- [ ] Full pipeline orchestration — webhook → filter → classify → context → draft → store
- [ ] Draft store & API — `GET /drafts`, `GET /drafts/{id}`, filter by issue key
- [ ] Approval workflow — `POST /approve`, `POST /reject` with feedback
- [ ] Tests — end-to-end pipeline tests via FastAPI TestClient

### 🔲 Phase 5: Notifications (`phase/step-5`)
- [ ] Notifications — optional Teams webhook cards + Email (SMTP) on draft generated / approved / rejected
- [ ] Tests — notification unit tests
- [ ] **Final:** 89 unit + integration tests, 78% code coverage

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
│   │   └── notifications.py      # Teams webhook + Email (SMTP) notifier
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

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — Component design and data flow
- [Setup Guide](docs/SETUP.md) — Installation, configuration, and testing
