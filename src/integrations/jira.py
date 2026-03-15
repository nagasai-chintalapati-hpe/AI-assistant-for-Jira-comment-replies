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
from typing import Optional, Any
import requests
from atlassian import Jira
import logging

logger = logging.getLogger(__name__)


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

    # Read helpers

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

    def get_last_comments(
        self, issue_key: str, n: int = 10
    ) -> list[dict[str, Any]]:
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
                        "status": (
                            related.get("fields", {})
                            .get("status", {})
                            .get("name", "")
                        ),
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
                        "author": (
                            entry.get("author", {}).get("displayName", "")
                        ),
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
            desc = fields.get("description", "") or ""
            urls.extend(self._extract_jenkins_urls(desc))

            # Check comments
            for c in fields.get("comment", {}).get("comments", []):
                urls.extend(self._extract_jenkins_urls(c.get("body", "")))

        except Exception as e:
            logger.error("Error detecting Jenkins links for %s: %s", issue_key, e)

        return list(set(urls))

    @staticmethod
    def _extract_jenkins_urls(text: str) -> list[str]:
        """Naive extraction of Jenkins console URLs from free text."""
        import re

        pattern = r"https?://[^\s\"'>]+/job/[^\s\"'>]+(?:console|consoleFull|consoleText)[^\s\"'>]*"
        return re.findall(pattern, text)

    # Write helpers

    def add_comment(
        self, issue_key: str, comment_body: str, is_internal: bool = False
    ) -> str:
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

    def update_custom_field(
        self, issue_key: str, field_id: str, value: str
    ) -> bool:
        """Update custom field (for draft storage)."""
        try:
            self.client.issue_update(issue_key, fields={field_id: value})
            return True
        except Exception as e:
            logger.error(
                "Error updating field %s on %s: %s", field_id, issue_key, e
            )
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

    def search_issues(
        self,
        jql: str,
        max_results: int = 100,
        fields: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Execute a JQL query and return matching issues.

        Parameters
        ----------
        jql : str
            JQL query string.
        max_results : int
            Maximum number of issues to return (capped at 100 per Jira page).
        fields : list[str] | None
            Specific field names to fetch.  Defaults to a useful subset for
            RAG ingestion (summary, description, status, resolution, comment,
            issuetype, priority).

        Returns
        -------
        list[dict]
            Raw Jira issue dictionaries from the search response.
        """
        _fields = fields or [
            "summary",
            "description",
            "status",
            "resolution",
            "comment",
            "issuetype",
            "priority",
        ]
        try:
            result = self.client.jql(jql, limit=max_results, fields=_fields)
            issues: list[dict[str, Any]] = result.get("issues", [])
            logger.info(
                "JQL search returned %d issues (query=%s…)", len(issues), jql[:80]
            )
            return issues
        except Exception as e:
            logger.error("Error executing JQL '%s…': %s", jql[:80], e)
            raise
