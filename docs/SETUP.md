# Setup Guide

## Prerequisites

- Python 3.10+
- Jira Cloud instance with API access
- Copilot SDK API key for AI-powered classification & refinement (optional)

## Installation

### 1. Clone & enter the project

```bash
cd AI-assistant-for-Jira-comment-replies
```

### 2. Create and activate virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -e .           # Production deps
pip install -e ".[dev]"    # + dev tools (black, ruff, mypy, pytest-asyncio)
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

#### Core Settings

| Variable | Required | Description |
|---|---|---|
| `JIRA_BASE_URL` | Yes (for live Jira) | e.g. `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | Yes (for live Jira) | Your Jira email |
| `JIRA_API_TOKEN` | Yes (for live Jira) | Generate in Jira → Personal Settings → API tokens |
| `COPILOT_API_KEY` | No | Leave blank for keyword-only mode |
| `COPILOT_MODEL` | No | Default: `gpt-4` |
| `APP_PORT` | No | Default: `8000` |
| `ASSISTANT_DB_PATH` | No | Default: `.data/assistant.db` |

#### Local LLM Settings

| Variable | Required | Description |
|---|---|---|
| `LLM_BACKEND` | No | `copilot` (default) or `local` for llama.cpp |
| `LLM_MODEL_PATH` | If `local` | Path to `.gguf` model file |
| `LLM_N_CTX` | No | Context window size (default: `4096`) |
| `LLM_N_GPU_LAYERS` | No | GPU layers (default: `0` = CPU only) |
| `LLM_TEMPERATURE` | No | Default: `0.1` |
| `LLM_MAX_TOKENS` | No | Default: `1024` |
| `LLM_N_THREADS` | No | Default: `4` |

#### RAG Settings

| Variable | Required | Description |
|---|---|---|
| `CHROMA_PERSIST_DIR` | No | Default: `.data/chroma` |
| `RAG_EMBEDDING_MODEL` | No | Default: `all-MiniLM-L6-v2` |
| `RAG_CHUNK_SIZE` | No | Default: `500` chars |
| `RAG_CHUNK_OVERLAP` | No | Default: `50` chars |
| `RAG_TOP_K` | No | Default: `5` snippets |
| `PDF_UPLOAD_DIR` | No | Default: `.data/pdfs` |

#### Confluence Settings

| Variable | Required | Description |
|---|---|---|
| `CONFLUENCE_BASE_URL` | No | Confluence Cloud URL |
| `CONFLUENCE_USERNAME` | No | Confluence email |
| `CONFLUENCE_API_TOKEN` | No | Confluence API token |
| `CONFLUENCE_SPACES` | No | Comma-separated space keys to index |
| `CONFLUENCE_LABELS` | No | Comma-separated labels to filter pages |

#### TestRail Settings

| Variable | Required | Description |
|---|---|---|
| `TESTRAIL_BASE_URL` | No | TestRail instance URL |
| `TESTRAIL_USERNAME` | No | TestRail email |
| `TESTRAIL_API_KEY` | No | TestRail API key |

#### Log Lookup Settings

| Variable | Required | Description |
|---|---|---|
| `JENKINS_BASE_URL` | No | Jenkins server URL |
| `JENKINS_USERNAME` | No | Jenkins username |
| `JENKINS_API_TOKEN` | No | Jenkins API token |
| `LOG_DIR` | No | Local log directory path |
| `LOG_TIME_WINDOW_HOURS` | No | Default: `24` |

#### Notification Settings

| Variable | Required | Description |
|---|---|---|
| `TEAMS_WEBHOOK_URL` | No | Teams incoming webhook URL for notifications |
| `SMTP_HOST` | No | SMTP server hostname (leave blank to disable email) |
| `SMTP_PORT` | No | Default: `587` |
| `SMTP_USERNAME` | No | SMTP login username |
| `SMTP_PASSWORD` | No | SMTP login password |
| `EMAIL_FROM` | No | Sender email address |
| `EMAIL_TO` | No | Comma-separated recipient addresses |

## Running the Application

### Development

```bash
uvicorn src.api.app:app --reload --host 0.0.0.0 --port 8000
```

### Verify it's running

```bash
curl http://localhost:8000/health
```

## Running Tests

```bash
# All tests with coverage
pytest

# Verbose output
pytest -v --tb=short

# Specific test file
pytest tests/unit/test_classifier.py

# Coverage report in browser
pytest --cov=src --cov-report=html
open htmlcov/index.html
```

## Registering a Jira Webhook

1. Go to **Jira Settings → System → Webhooks**
2. Click **Create a webhook**
3. Set URL: `https://<your-host>:8000/webhook/jira`
4. Select events: `comment_created`, `comment_updated`
5. Save

> **Tip:** For local development, use [ngrok](https://ngrok.com/) to expose your local server:
> ```bash
> ngrok http 8000
> ```
> Then use the ngrok URL in the Jira webhook configuration.

## Optional: RAG Dependencies

The RAG engine and document ingestion pipeline require additional packages
that are **not** installed by default (they are only needed if you use the
`/rag/*` endpoints):

```bash
pip install chromadb sentence-transformers pypdf
```

- **chromadb** — vector store for semantic retrieval
- **sentence-transformers** — embedding model (`all-MiniLM-L6-v2`)
- **pypdf** — PDF text extraction

The core pipeline (webhook → classify → context → draft) works without
these packages.  Tests mock all heavy dependencies so `pytest` runs
without installing them.

## Production Deployment (Docker)

The team never runs uvicorn manually. Docker handles starting, stopping, and
auto-restarting the service — including after server reboots.

### One-time setup on the server

```bash
# 1. Clone and enter the project
git clone <repo-url> && cd AI-assistant-for-Jira-comment-replies

# 2. Copy and fill in secrets
cp .env.example .env
nano .env   # set JIRA_BASE_URL, JIRA_USERNAME, JIRA_API_TOKEN, etc.

# 3. Build and start (runs in the background forever)
docker compose up -d --build
```

That's it. The container:
- **Auto-restarts** if it crashes (`restart: unless-stopped`)
- **Survives reboots** — Docker daemon starts it automatically on boot
- **Persists data** — the SQLite draft store and ChromaDB live in a Docker volume

### Day-to-day operations

| Task | Command |
|---|---|
| View live logs | `docker compose logs -f` |
| Check health | `curl http://localhost:8000/health` |
| Open Review UI | `http://<server-ip>:8000/ui` |
| Deploy a new version | `git pull && docker compose up -d --build` |
| Stop | `docker compose down` |
| Stop + wipe data | `docker compose down -v` |

### Register the Jira webhook (one-time per Jira project)

1. Jira → **Settings → System → WebHooks → Create WebHook**
2. **URL**: `https://<your-server>/webhook/jira`
3. **Events**: ☑ Issue → **Comment created**
4. Save

After that every real comment on any Jira issue flows through the pipeline
automatically — no scripts, no manual steps, no intervention required.

---

## Troubleshooting

### "Missing Jira configuration in environment variables"
- Ensure `JIRA_BASE_URL`, `JIRA_USERNAME`, and `JIRA_API_TOKEN` are set in `.env`
- This error only occurs when context collection tries to call live Jira — tests mock the client

### Tests fail with import errors
- Make sure you installed with `pip install -e .`
- Verify your venv is activated: `which python` should point to `.venv/bin/python`

### "Copilot SDK classification failed"
- Check `COPILOT_API_KEY` is valid
- The system gracefully falls back to keyword classification — no action required

### Notifications not sending
- **Teams:** verify `TEAMS_WEBHOOK_URL` is a valid incoming-webhook URL
- **Email:** ensure `SMTP_HOST`, `EMAIL_FROM`, and `EMAIL_TO` are all set
- Both channels are optional — the system works without them
