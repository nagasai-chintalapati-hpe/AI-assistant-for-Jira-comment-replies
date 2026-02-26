# Setup Guide

## Prerequisites

- Python 3.10 or later
- MongoDB (local or cloud instance)
- Jira Cloud account with admin access
- GitHub Copilot SDK API key
- (Optional) Confluence instance for RAG indexing

## Installation

### 1. Clone Repository

### 2. Create Virtual Environment
```bash
python3.10 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -e .
pip install -e ".[dev]"  # For development
```

### 4. Configure Environment
```bash
cp .env.example .env
# Edit .env with your credentials
```

### 5. Initialize Database
```bash
# Start MongoDB locally (or use cloud instance)
mongod

# In another terminal, initialize collections
python scripts/init_db.py
```

### 6. Register Jira Webhook
1. Go to Jira Settings > System > Webhooks
2. Click "Create a webhook"
3. Set URL: `https://your-domain.com:8000/webhook/jira`
4. Select events: `comment_created`, `comment_updated`
5. Copy webhook secret to `.env`

### 7. Index Knowledge Sources
```bash
# Index Confluence pages
python scripts/index_confluence.py
# Index API documentation
python scripts/index_api_docs.py
# Initialize Chroma database
python scripts/init_chroma.py
```

## Running the Application

### Development
```bash
# Terminal 1: Start FastAPI server
uvicorn src.api.app:app --reload --host 0.0.0.0 --port 8000
# Terminal 2: Monitor logs
tail -f logs/assistant.log
```

### Production
```bash
# Using Gunicorn
gunicorn -w 4 -k uvicorn.workers.UvicornWorker src.api.app:app
```

## Running Tests
```bash
# Run all tests
pytest
# Run with coverage
pytest --cov=src
# Run specific test file
pytest tests/unit/test_classifier.py
# Run in watch mode (requires pytest-watch)
ptw
```

## Configuration Details

### Jira Configuration
- `JIRA_BASE_URL`: Your Jira Cloud instance (e.g., https://org.atlassian.net)
- `JIRA_USERNAME`: Email associated with Jira account
- `JIRA_API_TOKEN`: API token (generate in Jira Personal Settings)

### LLM Configuration
- `GITHUB_COPILOT_API_KEY`: API key for GitHub Copilot SDK access
- `COPILOT_MODEL`: Model identifier used by Copilot SDK

### Webhook Configuration
- `WEBHOOK_SECRET`: HMAC secret for signature verification
- `WEBHOOK_PORT`: Port to listen on (default 8000)

### MongoDB Configuration
- `MONGODB_URI`: Connection string (local: mongodb://localhost:27017/jira-assistant)

## Verification
```bash
# Check API health
curl http://localhost:8000/health
# Test Jira connection
python -c "from src.integrations.jira import JiraClient; c = JiraClient(); print(c.client.myself())"
# Test MongoDB connection
python -c "from pymongo import MongoClient; c = MongoClient(); print(c.admin.command('ping'))"
# Run unit tests
pytest tests/unit/
```

## Troubleshooting
### "Jira API token invalid"
- Generate new token in Jira Personal Settings > Security > API tokens
- Verify username is email address

### "Webhook not receiving events"
- Check Jira webhook logs: Settings > System > Webhooks > [Your webhook]
- Verify URL is publicly accessible
- Check firewall rules

### "RAG queries returning no results"
- Run indexing scripts: `python scripts/index_confluence.py`
- Verify Confluence token has read access
- Check Chroma database: `python -c "from chromadb import Client; print(Client().list_collections())"`

### MongoDB connection issues
- Verify MongoDB is running: `mongo --version` and `mongod`
- Check connection string in `.env`
- For Atlas: ensure IP is whitelisted

## Next Steps
1. **Phase 2**: Implement LLM integration in `src/agent/drafter.py`
2. **Phase 3**: Build RAG indexing scripts
3. **Phase 4**: Add comprehensive test coverage
4. **Phase 5**: Deploy to staging environment

