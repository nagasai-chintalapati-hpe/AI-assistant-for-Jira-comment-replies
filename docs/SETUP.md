# Setup Guide

## Prerequisites

- Python 3.10+
- Jira Cloud instance with API access
- GitHub Copilot API key

## Install

```bash
git clone git@github.com:nagasai-chintalapati-hpe/AI-assistant-for-Jira-comment-replies.git && cd AI-assistant-for-Jira-comment-replies
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure

```bash
cp .env.example .env
```

### Required

| Variable | Description |
|----------|-------------|
| `JIRA_BASE_URL` | `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Service-account email |
| `JIRA_API_TOKEN` | Jira → Personal Settings → API tokens |

### LLM

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `copilot` | `copilot` / `local` / `none` |
| `COPILOT_API_KEY` | — | GitHub Copilot API key |
| `COPILOT_MODEL` | `gpt-4o` | Model name |
| `LLM_MODEL_PATH` | — | `.gguf` file path (local backend) |

### RAG

| Variable | Default | Description |
|----------|---------|-------------|
| `CHROMA_PERSIST_DIR` | `.data/chroma` | ChromaDB storage |
| `RAG_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Embedding model |
| `RAG_TOP_K` | `5` | Snippets per query |

### Integrations (all optional)

| Variable | Description |
|----------|-------------|
| `CONFLUENCE_BASE_URL` / `CONFLUENCE_API_TOKEN` | Confluence Cloud |
| `TESTRAIL_BASE_URL` / `TESTRAIL_API_KEY` | TestRail instance |
| `GIT_PROVIDER` / `GIT_TOKEN` | `github` / `gitlab` / `bitbucket` + PAT |
| `GIT_REPOS` | Comma-separated repos for multi-repo PR search |
| `JENKINS_BASE_URL` / `JENKINS_API_TOKEN` | Jenkins server |
| `ELK_HOST` / `ELK_API_KEY` | Elasticsearch / OpenSearch |
| `S3_BUCKET` / `S3_ENDPOINT_URL` | S3 or MinIO |
| `TEAMS_WEBHOOK_URL` | Teams incoming webhook |
| `SMTP_HOST` / `EMAIL_FROM` / `EMAIL_TO` | Email notifications |

### Infrastructure (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_RPM` | `60` | Webhook rate limit per IP |
| `REDIS_ENABLED` | `false` | Distributed rate-limit state |
| `QUEUE_ENABLED` | `false` | Async processing via RabbitMQ |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost/` | AMQP URL |
| `DASHBOARD_TOKEN` | — | Lock dashboard behind a shared token |
| `JIRA_WEBHOOK_SECRET` | — | HMAC secret (set `VALIDATE_WEBHOOK_SIGNATURE=true`) |

## Run

### Docker (production)

```bash
docker compose up -d --build
```

The container auto-restarts on crash/reboot and persists data via Docker volumes.

| Task | Command |
|------|---------|
| View logs | `docker compose logs -f` |
| Health check | `curl http://localhost:8000/health` |
| Review UI | `http://localhost:8000/ui` |
| Dashboard | `http://localhost:8000/dashboard` |
| Deploy update | `git pull && docker compose up -d --build` |
| Stop | `docker compose down` |

## Register Jira Webhook

1. Jira → **Settings → System → WebHooks → Create**
2. URL: `https://<your-host>/webhook/jira`
3. Events: ☑ Comment created
4. Save

## Tests

```bash
pytest tests/unit/ -v              # unit tests
pytest tests/unit/ --cov=src       # with coverage
pytest tests/integration/ -v       # requires live credentials
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Webhook 400 (signature mismatch) | Set `VALIDATE_WEBHOOK_SIGNATURE=false` for local dev |
| ChromaDB empty after ingest | Check `CHROMA_PERSIST_DIR` is writable |
| Confidence always 0.0 | Set `LLM_BACKEND=copilot` and add `COPILOT_API_KEY` |
| Teams card not appearing | Verify `TEAMS_WEBHOOK_URL` is an Incoming Webhook URL |
| Import errors | Run `pip install -e .` and confirm venv is active |
