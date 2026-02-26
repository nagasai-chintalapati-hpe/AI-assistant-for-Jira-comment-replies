# AI Assistant for Jira Comment Replies

**Intern Project 2026** - Nagasai & Yousef

An agentic AI assistant that generates context-aware draft replies to developer comments on Jira defects, grounded in internal knowledge sources.

## Project Vision

Reduce defect triage time by auto-drafting high-quality, evidence-based responses while maintaining strong human-in-the-loop controls, security, and auditability.

## Key Features

- **Event-Driven**: Detects new comments on Jira defects via webhooks
- **Smart Classification**: Categorizes comments (Cannot reproduce, Need logs, As designed, etc.)
- **RAG-Grounded**: Retrieves evidence from Confluence, API docs, TestRail, Git PRs, logs
- **Draft Generation**: Templated, context-aware response drafting
- **Human Approval**: Draft stored in Jira + Approve/Post workflow
- **Action Suggestions**: Auto-suggest labels, transitions, assignments
- **Audit Trail**: Full traceability of evidence, approvals, and postings
- **On-Premise Ready**: Runs on-prem for security and compliance

## Architecture

```
Jira Cloud (Webhook)
    ↓
Event Trigger
    ↓
Classifier (Intent + Context)
    ↓
Context Collection (Issue, PR, TestRail, Logs)
    ↓
RAG Pipeline (Confluence, Docs, API refs)
    ↓
LLM (Draft generation)
    ↓
Approval Workflow (Jira UI + Teams)
    ↓
Action Executor (Post + Log)
```

## Project Structure

```
├── src/
│   ├── agent/              # Core agent logic
│   │   ├── classifier.py   # Comment classification
│   │   ├── context_collector.py  # Gather issue context
│   │   ├── rag_pipeline.py # RAG retrieval
│   │   └── drafter.py      # Draft generation
│   ├── integrations/       # External service integrations
│   │   ├── jira/
│   │   ├── confluence/
│   │   ├── testrail/
│   │   └── git/
│   ├── api/                # FastAPI webhook & approval endpoints
│   ├── models/             # Data models & schemas
│   ├── storage/            # MongoDB for drafts & audit logs
│   ├── knowledge/          # RAG indexing & retrieval
│   └── utils/              # Helpers, logging, config
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docs/                   # Architecture, setup, runbooks
├── .env.example
├── pyproject.toml
└── README.md
```

## Success Metrics

- % of drafts accepted with minimal edits
- Time saved per defect triage
- Reduction in back-and-forth comment cycles
- Hallucination rate (claims without evidence)
- Developer satisfaction rating

## Getting Started

### Prerequisites

- Python 3.10+
- MongoDB (for audit logs)
- Jira Cloud instance with admin access
- GitHub Copilot SDK API key
- (Optional) Confluence instance for RAG

### Installation

```bash
pip install -e .
```

### Configuration

1. Copy `.env.example` to `.env` and fill in your credentials
2. Configure Jira webhook to point to your deployment
3. Index internal knowledge sources (Confluence, API docs)

### Running Tests

```bash
pytest
```

### Development

```bash
pip install -e ".[dev]"
black src/ tests/
ruff check src/ tests/
mypy src/
```

## Status

🚧 **Phase 1: Architecture & Scaffolding** (Current)
- [x] Project structure setup
- [x] Core models & schemas
- [x] Jira integration (webhook receiver, API client)
- [x] Event handling pipeline

📋 **Phase 2: Agent Core**
- [ ] Comment classifier (LLM-based)
- [ ] Context collector (enhanced)
- [ ] RAG pipeline (Chroma + LangChain)
- [ ] Draft generator with templates

📋 **Phase 3: Approval & Execution**
- [ ] Jira UI draft storage
- [ ] Approval workflow
- [ ] Action executor
- [ ] Audit logging

📋 **Phase 4: Knowledge Indexing**
- [ ] Confluence scraper & indexer
- [ ] API docs ingestion
- [ ] TestRail integration
- [ ] Git PR indexer

📋 **Phase 5: Teams Integration (Optional)**
- [ ] Adaptive Card generation
- [ ] Approval card handler
- [ ] Teams notifications

📋 **Phase 6: Testing & Deployment**
- [ ] Comprehensive test suite
- [ ] Integration tests
- [ ] On-prem deployment guide
- [ ] Monitoring & observability

## Documentation

- **[Architecture](docs/ARCHITECTURE.md)** - System design and component overview
- **[Setup Guide](docs/SETUP.md)** - Installation and configuration instructions


