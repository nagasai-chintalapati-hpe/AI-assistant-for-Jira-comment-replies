"""Jira Cloud integration – full issue + comment retrieval.

Provides:
  • get_issue          – full issue JSON
  • get_comments       – all comments on an issue
  • get_last_comments  – last N comments (default 10)
  • get_attachments    – attachment metadata list
  • get_linked_issues  – linked issue keys with relationship type
  • get_changelog      – issue changelog (status transitions, etc.)
  • add_comment        – post a (optionally internal) comment
  • update_custom_field / add_label / transition_issue – mutations
"""

import os
import re
from typing import Any, Optional

import requests
from atlassian import Jira

logger = logging.getLogger(__name__)


def _adf_to_text(node: Any) -> str:
    """Recursively convert Atlassian Document Format (ADF) to plain text.

    Jira Cloud API v3 returns description/comment bodies as ADF dicts.
    This flattens them so keyword matching, classification, and templates
    all work on readable strings.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return str(node) if node else ""

    parts: list[str] = []
    ntype = node.get("type", "")

    if ntype == "text":
        parts.append(node.get("text", ""))
    elif ntype in ("hardBreak",):
        parts.append("\n")

    for child in node.get("content", []):
        parts.append(_adf_to_text(child))

    # block-level nodes get a trailing newline for readability
    if ntype in ("paragraph", "heading", "bulletList", "orderedList",
                 "listItem", "codeBlock", "blockquote", "rule"):
        return "\n".join(p for p in parts if p) + "\n"

    return " ".join(p for p in parts if p)


def _ensure_text(value: Any) -> str:
    """Return *value* as plain text, converting ADF dicts if needed."""
    if isinstance(value, dict):
        return _adf_to_text(value).strip()
    return str(value) if value else ""


class JiraClient:
    """Client for Jira Cloud API interactions"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        self.base_url = base_url or os.getenv("JIRA_BASE_URL")
        self.username = username or os.getenv("JIRA_USERNAME")
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN")

        if not all([self.base_url, self.username, self.api_token]):
            raise ValueError("Missing Jira configuration in environment variables")

        self.client = Jira(
            url=self.base_url,
            username=self.username,
            password=self.api_token,
        )

    #  Read helpers
    def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Get full issue JSON including all standard and custom fields."""
        try:
            return self.client.issue(issue_key)
        except Exception as e:
            logger.error("Error fetching issue %s: %s", issue_key, e)
            raise

    def get_comments(self, issue_key: str) -> list[dict[str, Any]]:
        """Return every comment on *issue_key* in chronological order."""
        try:
            issue = self.get_issue(issue_key)
            return issue.get("fields", {}).get("comment", {}).get("comments", [])
        except Exception as e:
            logger.error("Error fetching comments for %s: %s", issue_key, e)
            return []

    def get_last_comments(self, issue_key: str, n: int = 10) -> list[dict[str, Any]]:
        """Return the last *n* comments on *issue_key* (newest last)."""
        all_comments = self.get_comments(issue_key)
        return all_comments[-n:]

    def get_attachments(self, issue_key: str) -> list[dict[str, Any]]:
        """Return attachment metadata (filename, url, mimeType, size)."""
        try:
            issue = self.get_issue(issue_key)
            fields = issue.get("fields", {})
            attachments = []
            for att in fields.get("attachment", []):
                attachments.append(
                    {
                        "id": att.get("id", ""),
                        "filename": att.get("filename", ""),
                        "content_url": att.get("content", ""),
                        "mime_type": att.get("mimeType", ""),
                        "size": att.get("size", 0),
                        "created": att.get("created", ""),
                    }
                )
            return attachments
        except Exception as e:
            logger.error("Error fetching attachments for %s: %s", issue_key, e)
            return []

    def get_linked_issues(self, issue_key: str) -> list[dict[str, str]]:
        """
        Return linked issues with their relationship type.

        Each entry: {"key", "type", "direction", "status"}
        """
        try:
            issue = self.get_issue(issue_key)
            links = issue.get("fields", {}).get("issuelinks", [])
            result: list[dict[str, str]] = []
            for link in links:
                link_type = link.get("type", {}).get("name", "")
                # Jira models links with inward / outward halves
                if "inwardIssue" in link:
                    related = link["inwardIssue"]
                    direction = "inward"
                elif "outwardIssue" in link:
                    related = link["outwardIssue"]
                    direction = "outward"
                else:
                    continue
                result.append(
                    {
                        "key": related.get("key", ""),
                        "type": link_type,
                        "direction": direction,
                        "status": (related.get("fields", {}).get("status", {}).get("name", "")),
                    }
                )
            return result
        except Exception as e:
            logger.error("Error fetching linked issues for %s: %s", issue_key, e)
            return []

    def get_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """
        Return the changelog (history) for *issue_key*.

        Each entry: {"author", "created", "items": [{field, from, to}]}
        """
        try:
            issue = self.get_issue(issue_key)
            changelog = issue.get("changelog", {}).get("histories", [])
            result = []
            for entry in changelog:
                items = []
                for item in entry.get("items", []):
                    items.append(
                        {
                            "field": item.get("field", ""),
                            "from": item.get("fromString", ""),
                            "to": item.get("toString", ""),
                        }
                    )
                result.append(
                    {
                        "author": (entry.get("author", {}).get("displayName", "")),
                        "created": entry.get("created", ""),
                        "items": items,
                    }
                )
            return result
        except Exception as e:
            logger.error("Error fetching changelog for %s: %s", issue_key, e)
            return []

    def detect_jenkins_links(self, issue_key: str) -> list[str]:
        """
        Scan the issue description, comments, and remote-links for
        Jenkins console URLs.
        """
        urls: list[str] = []
        try:
            issue = self.get_issue(issue_key)
            fields = issue.get("fields", {})

            # Check description
            desc = _ensure_text(fields.get("description", ""))
            urls.extend(self._extract_jenkins_urls(desc))

            # Check comments
            for c in fields.get("comment", {}).get("comments", []):
                urls.extend(self._extract_jenkins_urls(_ensure_text(c.get("body", ""))))

        except Exception as e:
            logger.error("Error detecting Jenkins links for %s: %s", issue_key, e)

        return list(set(urls))

    @staticmethod
    def _extract_jenkins_urls(text: str) -> list[str]:
        """Naive extraction of Jenkins console URLs from free text."""
        import re

        pattern = r"https?://[^\s\"'>]+/job/[^\s\"'>]+(?:console|consoleFull|consoleText)[^\s\"'>]*"
        return re.findall(pattern, text)

    # Extraction helpers (operate on pre-fetched issue data)

    @staticmethod
    def extract_attachments(issue_data: dict) -> list[dict[str, Any]]:
        """Extract attachment metadata from a pre-fetched issue dict."""
        fields = issue_data.get("fields", {})
        attachments = []
        for att in fields.get("attachment", []):
            attachments.append(
                {
                    "id": att.get("id", ""),
                    "filename": att.get("filename", ""),
                    "content_url": att.get("content", ""),
                    "mime_type": att.get("mimeType", ""),
                    "size": att.get("size", 0),
                    "created": att.get("created", ""),
                }
            )
        return attachments

    @staticmethod
    def extract_linked_issues(issue_data: dict) -> list[dict[str, str]]:
        """Extract linked issues from a pre-fetched issue dict."""
        links = issue_data.get("fields", {}).get("issuelinks", [])
        result: list[dict[str, str]] = []
        for link in links:
            link_type = link.get("type", {}).get("name", "")
            if "inwardIssue" in link:
                related = link["inwardIssue"]
                direction = "inward"
            elif "outwardIssue" in link:
                related = link["outwardIssue"]
                direction = "outward"
            else:
                continue
            result.append(
                {
                    "key": related.get("key", ""),
                    "type": link_type,
                    "direction": direction,
                    "status": (related.get("fields", {}).get("status", {}).get("name", "")),
                }
            )
        return result

    @staticmethod
    def extract_changelog(issue_data: dict) -> list[dict[str, Any]]:
        """Extract changelog from a pre-fetched issue dict."""
        changelog = issue_data.get("changelog", {}).get("histories", [])
        result = []
        for entry in changelog:
            items = []
            for item in entry.get("items", []):
                items.append(
                    {
                        "field": item.get("field", ""),
                        "from": item.get("fromString", ""),
                        "to": item.get("toString", ""),
                    }
                )
            result.append(
                {
                    "author": (entry.get("author", {}).get("displayName", "")),
                    "created": entry.get("created", ""),
                    "items": items,
                }
            )
        return result

    @staticmethod
    def extract_jenkins_links(issue_data: dict) -> list[str]:
        """Extract Jenkins URLs from a pre-fetched issue dict."""
        pattern = r"https?://[^\s\"'>]+/job/[^\s\"'>]+(?:console|consoleFull|consoleText)[^\s\"'>]*"
        urls: list[str] = []
        fields = issue_data.get("fields", {})

        desc = _ensure_text(fields.get("description", ""))
        urls.extend(re.findall(pattern, desc))

        for c in fields.get("comment", {}).get("comments", []):
            body = _ensure_text(c.get("body", ""))
            urls.extend(re.findall(pattern, body))

        return list(set(urls))

    # Jenkins console log fetching

    @staticmethod
    def fetch_jenkins_console(
        url: str,
        *,
        max_chars: int = 3000,
        timeout: int = 10,
    ) -> Optional[str]:
        """
        Fetch the last *max_chars* of a Jenkins console log.

        Accepts any Jenkins build URL (console, consoleFull, consoleText)
        and normalises it to the ``/consoleText`` plain-text endpoint.

        Returns ``None`` on any network / auth error so the pipeline
        can continue without blocking.
        """
        import re as _re

        # Normalise to /consoleText
        normalised = _re.sub(
            r"/(console|consoleFull|consoleText)(/?)$",
            "/consoleText",
            url.rstrip("/"),
        )
        if not normalised.endswith("/consoleText"):
            normalised += "/consoleText"

        try:
            resp = requests.get(normalised, timeout=timeout, verify=False)
            resp.raise_for_status()
            text = resp.text
            # Return the tail (most useful part of a build log)
            if len(text) > max_chars:
                text = "... (truncated)\n" + text[-max_chars:]
            return text
        except Exception as exc:
            logger.warning("Failed to fetch Jenkins log %s: %s", normalised, exc)
            return None

    def fetch_jenkins_logs(
        self,
        urls: list[str],
        *,
        max_chars: int = 3000,
        timeout: int = 10,
    ) -> dict[str, str]:
        """
        Fetch console logs for a list of Jenkins URLs.

        Returns a dict mapping each URL to its (possibly truncated)
        console output.  URLs that fail are silently omitted.
        """
        results: dict[str, str] = {}
        for url in urls:
            content = self.fetch_jenkins_console(
                url,
                max_chars=max_chars,
                timeout=timeout,
            )
            if content:
                results[url] = content
        return results

    # Write helpers
    def add_comment(self, issue_key: str, comment_body: str, is_internal: bool = False) -> str:
        """Post a comment to an issue (optionally internal)."""
        try:
            payload: dict[str, Any] = {"body": comment_body}
            if is_internal:
                payload["visibility"] = {"type": "role", "value": "Developers"}

            response = self.client.issue_add_comment(issue_key, comment_body)
            logger.info("Posted comment to %s", issue_key)
            return response.get("id", "")
        except Exception as e:
            logger.error("Error posting comment to %s: %s", issue_key, e)
            raise

    def update_custom_field(self, issue_key: str, field_id: str, value: str) -> bool:
        """Update custom field (for draft storage)."""
        try:
            self.client.issue_update(issue_key, fields={field_id: value})
            return True
        except Exception as e:
            logger.error("Error updating field %s on %s: %s", field_id, issue_key, e)
            return False

    def add_label(self, issue_key: str, label: str) -> bool:
        """Add label to issue (idempotent)."""
        try:
            issue = self.get_issue(issue_key)
            labels = issue.get("fields", {}).get("labels", [])
            if label not in labels:
                labels.append(label)
                self.client.issue_update(issue_key, fields={"labels": labels})
            return True
        except Exception as e:
            logger.error("Error adding label to %s: %s", issue_key, e)
            return False

    def transition_issue(self, issue_key: str, transition_id: str) -> bool:
        """Transition issue to a new status."""
        try:
            self.client.issue_transition(issue_key, transition_id)
            return True
        except Exception as e:
            logger.error("Error transitioning %s: %s", issue_key, e)
            return False
