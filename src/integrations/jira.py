"""Jira Cloud integration"""

import os
from typing import Optional, Any
import requests
from atlassian import Jira
import logging

logger = logging.getLogger(__name__)


class JiraClient:
    """Client for Jira Cloud API interactions"""

    def __init__(self):
        self.base_url = os.getenv("JIRA_BASE_URL")
        self.username = os.getenv("JIRA_USERNAME")
        self.api_token = os.getenv("JIRA_API_TOKEN")
        
        if not all([self.base_url, self.username, self.api_token]):
            raise ValueError("Missing Jira configuration in environment variables")
        
        self.client = Jira(
            url=self.base_url,
            username=self.username,
            password=self.api_token,
        )
    
    def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Get issue details"""
        try:
            return self.client.issue_get(issue_key)
        except Exception as e:
            logger.error(f"Error fetching issue {issue_key}: {e}")
            raise
    
    def get_comments(self, issue_key: str) -> list[dict[str, Any]]:
        """Get all comments for an issue"""
        try:
            issue = self.get_issue(issue_key)
            return issue.get("fields", {}).get("comment", {}).get("comments", [])
        except Exception as e:
            logger.error(f"Error fetching comments for {issue_key}: {e}")
            return []
    
    def add_comment(self, issue_key: str, comment_body: str, is_internal: bool = False) -> str:
        """Post a comment to an issue"""
        try:
            payload = {
                "body": comment_body,
            }
            if is_internal:
                payload["visibility"] = {
                    "type": "role",
                    "value": "Developers"
                }
            
            response = self.client.issue_add_comment(issue_key, comment_body)
            logger.info(f"Posted comment to {issue_key}")
            return response.get("id", "")
        except Exception as e:
            logger.error(f"Error posting comment to {issue_key}: {e}")
            raise
    
    def update_custom_field(self, issue_key: str, field_id: str, value: str) -> bool:
        """Update custom field (for draft storage)"""
        try:
            self.client.issue_update(
                issue_key,
                fields={field_id: value}
            )
            return True
        except Exception as e:
            logger.error(f"Error updating field {field_id} on {issue_key}: {e}")
            return False
    
    def add_label(self, issue_key: str, label: str) -> bool:
        """Add label to issue"""
        try:
            issue = self.get_issue(issue_key)
            labels = issue.get("fields", {}).get("labels", [])
            if label not in labels:
                labels.append(label)
                self.client.issue_update(
                    issue_key,
                    fields={"labels": labels}
                )
            return True
        except Exception as e:
            logger.error(f"Error adding label to {issue_key}: {e}")
            return False
    
    def transition_issue(self, issue_key: str, transition_id: str) -> bool:
        """Transition issue to a new status"""
        try:
            self.client.issue_transition(issue_key, transition_id)
            return True
        except Exception as e:
            logger.error(f"Error transitioning {issue_key}: {e}")
            return False
