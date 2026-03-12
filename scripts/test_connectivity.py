"""Quick connectivity test for Jira + Confluence APIs."""

from dotenv import load_dotenv
load_dotenv(".env", override=True)

import os
import json

print("=" * 60)
print("JIRA — nagasai42006.atlassian.net")
print("=" * 60)

from atlassian import Jira

jira = Jira(
    url=os.getenv("JIRA_BASE_URL"),
    username=os.getenv("JIRA_USERNAME"),
    password=os.getenv("JIRA_API_TOKEN"),
)

try:
    projects = jira.projects(included_archived=None)
    print(f"Projects accessible: {len(projects)}")
    for p in projects[:10]:
        print(f"  [{p.get('key')}] {p.get('name')}")
except Exception as e:
    print(f"Project list failed: {e}")

print()
print("--- Fetching IP-7 ---")
try:
    issue = jira.issue("IP-7")
    fields = issue.get("fields", {})
    print(f"  Summary:  {fields.get('summary')}")
    print(f"  Type:     {fields.get('issuetype', {}).get('name')}")
    print(f"  Status:   {fields.get('status', {}).get('name')}")
    print(f"  Priority: {fields.get('priority', {}).get('name')}")
    desc = fields.get("description") or ""
    if isinstance(desc, dict):
        # Jira v3 ADF format
        print(f"  Desc:     (ADF document)")
    else:
        print(f"  Desc:     {str(desc)[:200]}")
    comments = fields.get("comment", {}).get("comments", [])
    print(f"  Comments: {len(comments)}")
    for c in comments[:5]:
        author = c.get("author", {}).get("displayName", "unknown")
        body = c.get("body", "")
        if isinstance(body, dict):
            body = "(ADF)"
        else:
            body = str(body)[:120]
        print(f"    [{author}]: {body}")
    print("\n  JIRA: OK")
except Exception as e:
    print(f"  JIRA FAILED: {e}")

print()
print("=" * 60)
print("CONFLUENCE — hpe.atlassian.net")
print("=" * 60)

try:
    from atlassian import Confluence

    conf = Confluence(
        url=os.getenv("CONFLUENCE_BASE_URL"),
        username=os.getenv("CONFLUENCE_USERNAME"),
        password=os.getenv("CONFLUENCE_API_TOKEN"),
        cloud=True,
    )

    space_key = os.getenv("CONFLUENCE_SPACES", "")
    print(f"Space: {space_key}")

    cql = f'type=page AND space="{space_key}"'
    results = conf.cql(cql, limit=10)
    pages = results.get("results", [])
    print(f"Pages found: {len(pages)}")
    for p in pages:
        content = p.get("content", p)
        print(f"  [{content.get('id')}] {content.get('title')}")

    print("\n  CONFLUENCE: OK")
except Exception as e:
    print(f"  CONFLUENCE FAILED: {e}")

print()
print("DONE")
