# Architecture Overview

## High-Level Flow

```
1. Jira Webhook Event
   └─> Webhook Receiver (FastAPI)

2. Event Processing
   ├─> Parse & Validate
   ├─> Classify Intent
   └─> Collect Context

3. RAG Pipeline
   ├─> Query Confluence
   ├─> Query API Docs
   ├─> Query Git/PRs
   └─> Query TestRail

4. Draft Generation
   ├─> Load Template
   ├─> Call LLM (GitHub Copilot SDK)
   ├─> Extract Citations
   └─> Suggest Actions/Labels

5. Storage & Approval
   ├─> Store Draft in MongoDB
   ├─> Store in Jira Custom Field
   └─> Await Human Approval

6. Post & Audit
   ├─> Post to Jira (on approval)
   ├─> Log Audit Trail
   └─> Update Metrics
```

## Components

### 1. **Webhook Receiver** (`src/api/app.py`)
- FastAPI server listening for Jira webhook events
- Validates webhook signatures
- Routes events to processing pipeline
- Endpoints:
  - `POST /webhook/jira` - Receive comment events
  - `POST /approve` - Approve draft
  - `POST /reject` - Reject draft
  - `GET /health` - Health check

### 2. **Comment Classifier** (`src/agent/classifier.py`)
- Classifies comments into predefined types
- Uses LLM for intent detection
- Identifies missing context
- Suggests clarifying questions

**Comment Types:**
- Cannot reproduce
- Need more info (logs, env, steps)
- As designed / Expected behavior
- Duplicate / Already fixed
- Not a bug / Configuration issue
- Fix ready—please validate
- Blocked by dependency
- Status update

### 3. **Context Collector** (`src/agent/context_collector.py`)
- Fetches issue details from Jira
- Extracts versions, components, labels
- Retrieves issue history
- Collects linked issues, attachments
- Integrates Git PR metadata
- Queries TestRail results

### 4. **RAG Pipeline** (`src/agent/rag_pipeline.py`)
- **Confluence Indexing**: Scrapes and indexes VME docs
- **API Documentation**: Index API references
- **Git Integration**: Retrieve related PRs and commits
- **TestRail**: Query test results by run ID
- **Log Retrieval**: Access build/error logs
- Uses **Chroma** for vector similarity search
- Tracks evidence sources for citations

### 5. **Response Drafter** (`src/agent/drafter.py`)
- Generates contextualized responses using GitHub Copilot SDK
- Loads templates based on comment classification
- Builds prompts with context + RAG results
- Extracts suggested actions (labels, transitions, assignments)
- Generates citations to evidence sources
- Tracks confidence scores

### 6. **Storage Layer**
- **MongoDB**: Draft storage, audit logs, metrics
- **Jira Custom Field**: Draft in Jira UI for in-context approval
- **Teams**: (Optional) Adaptive Card approval storage

### 7. **Integration Services**
- **Jira Client**: REST API interactions
- **Confluence Client**: Knowledge base queries
- **Git Client**: PR and commit information
- **TestRail Client**: Test result queries

## Data Models

### Comment
```python
{
  "comment_id": str,
  "issue_key": str,
  "author": str,
  "author_role": str,
  "body": str,
  "created": datetime,
}
```

### Classification
```python
{
  "comment_id": str,
  "comment_type": CommentType,
  "confidence": float,
  "reasoning": str,
  "missing_context": [str],
  "suggested_questions": [str],
}
```

### Context
```python
{
  "issue_key": str,
  "summary": str,
  "description": str,
  "environment": str,
  "versions": [str],
  "components": [str],
  "linked_issues": [{}],
  "rag_results": [{source, content, relevance}],
}
```

### Draft
```python
{
  "draft_id": str,
  "issue_key": str,
  "in_reply_to_comment_id": str,
  "body": str,
  "status": DraftStatus,  # generated, approved, posted, rejected
  "suggested_labels": [str],
  "suggested_actions": [{action, value}],
  "citations": [{source, url, excerpt}],
  "confidence_score": float,
  "approved_by": str,
  "approved_at": datetime,
}
```

## Approval Workflows

### 1. Jira UI (Preferred)
1. Draft stored in custom Jira field
2. Developer sees in-context in issue view
3. Click "Approve" or "Reject"
4. Approval triggers comment posting
5. Audit logged in field

### 2. Teams Adaptive Card (Optional)
1. Draft formatted as Teams card
2. Approval request posted to channel
3. Click "Approve" or "Request Changes"
4. Response triggers Jira update

## Security Considerations

- **Webhook Validation**: HMAC-SHA256 signature verification
- **Secrets Management**: Credentials in vault (not in code)
- **Least Privilege**: Jira API token with minimal scopes
- **Audit Logging**: All actions logged with user/timestamp
- **Input Validation**: Pydantic models for type safety
- **Rate Limiting**: Prevent abuse of LLM endpoints

## Deployment

### Prerequisites
- Python 3.10+
- MongoDB instance
- Jira Cloud instance
- GitHub Copilot SDK API key
- (Optional) Confluence instance

### On-Prem Deployment
1. Clone repo and install dependencies
2. Configure `.env` with credentials
3. Initialize Chroma database
4. Index knowledge sources
5. Run FastAPI server: `uvicorn src.api.app:app`
6. Register webhook in Jira

### Docker Deployment
```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install -e .
COPY src/ src/
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0"]
```

## Future improvements

- [ ] Multi-turn conversation with Jira for clarifications
- [ ] Custom templates per project/component
- [ ] Learning from approved/rejected drafts
- [ ] Integration with GitHub Actions for log retrieval
- [ ] Dashboard for metrics and trends
- [ ] Slack approval workflow
