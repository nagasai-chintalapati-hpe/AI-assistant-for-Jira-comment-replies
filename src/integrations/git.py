"""Git provider client — GitHub, GitLab, Bitbucket (REST API).

Fetches Pull Request metadata to ground draft responses with:
  • Commit SHA, merge status, branch names
  • PR title, description, author
  • Merge timestamp

PR reference detection:
  Scans Jira issue body / comments for patterns:
    - GitHub:    PR #123  /  github.com/.../pull/123
    - GitLab:    MR !123  /  gitlab.com/.../merge_requests/123
    - Bitbucket: PR-123   /  bitbucket.org/.../pull-requests/123

Auth: Bearer token via GIT_TOKEN env var.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests
from requests.exceptions import HTTPError, Timeout

from src.config import settings
from src.models.context import GitPRMetadata

logger = logging.getLogger(__name__)

_GITHUB_PR_PATTERNS = [
    re.compile(r"(?:PR|pull[_\s]?request)[#\s]+(\d+)", re.IGNORECASE),
    re.compile(r"github\.com/[^/]+/[^/]+/pull/(\d+)", re.IGNORECASE),
    re.compile(r"(?<!\w)#(\d{1,6})(?!\w)"),           # bare #123 fallback
]
_GITLAB_MR_PATTERNS = [
    re.compile(r"(?:MR|merge[_\s]?request)[!\s]+(\d+)", re.IGNORECASE),
    re.compile(r"gitlab\.com/[^/]+/[^/]+/-/merge_requests/(\d+)", re.IGNORECASE),
]
_BITBUCKET_PR_PATTERNS = [
    re.compile(r"PR-(\d+)", re.IGNORECASE),
    re.compile(r"bitbucket\.org/[^/]+/[^/]+/pull-requests/(\d+)", re.IGNORECASE),
]


class GitClient:
    """REST API client for GitHub, GitLab, and Bitbucket."""

    def __init__(
        self,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> None:
        self._provider = (provider if provider is not None else settings.git.provider).lower()
        self._token = token if token is not None else settings.git.token
        self._owner = owner if owner is not None else settings.git.owner
        self._repo = repo if repo is not None else settings.git.repo

        # Base URL: use override or provider default
        if base_url:
            self._base_url = base_url.rstrip("/")
        elif settings.git.base_url:
            self._base_url = settings.git.base_url.rstrip("/")
        else:
            self._base_url = self._default_base_url()

        self._session = requests.Session()
        if self._token:
            if self._provider == "gitlab":
                self._session.headers["PRIVATE-TOKEN"] = self._token
            else:
                self._session.headers["Authorization"] = f"Bearer {self._token}"
        self._session.headers["Accept"] = "application/vnd.github+json"
        self._session.headers["X-GitHub-Api-Version"] = "2022-11-28"

        if self.enabled:
            logger.info(
                "GitClient ready (provider=%s, owner=%s, repo=%s)",
                self._provider, self._owner, self._repo,
            )

    # Properties

    @property
    def enabled(self) -> bool:
        """True when token + owner are configured."""
        return bool(self._token and self._owner)

    @property
    def provider(self) -> str:
        return self._provider

    # Public API

    def get_pr(
        self,
        pr_number: int,
        repo: Optional[str] = None,
    ) -> GitPRMetadata:
        """Fetch a Pull/Merge Request by number.

        Args:
            pr_number: The PR / MR number.
            repo: Repository name (owner/repo or just repo).
                  Falls back to ``settings.git.repo``.

        Returns:
            GitPRMetadata with normalised fields.
        """
        resolved_repo = self._resolve_repo(repo)
        if self._provider == "github":
            return self._get_github_pr(pr_number, resolved_repo)
        elif self._provider == "gitlab":
            return self._get_gitlab_mr(pr_number, resolved_repo)
        elif self._provider == "bitbucket":
            return self._get_bitbucket_pr(pr_number, resolved_repo)
        else:
            raise ValueError(f"Unsupported Git provider: {self._provider!r}")

    def get_pr_by_branch(
        self,
        branch: str,
        repo: Optional[str] = None,
    ) -> Optional[GitPRMetadata]:
        """Find an open PR for *branch* (head branch lookup).

        Returns the first matching open PR, or None if not found.
        Only supported for GitHub.
        """
        resolved_repo = self._resolve_repo(repo)
        if self._provider != "github":
            logger.debug("get_pr_by_branch only supported for GitHub, skipping")
            return None

        owner, name = self._split_repo(resolved_repo)
        url = f"{self._base_url}/repos/{owner}/{name}/pulls"
        try:
            data = self._get(url, params={"state": "open", "head": f"{owner}:{branch}"})
            if data:
                return self._parse_github_pr(data[0], resolved_repo)
        except Exception as exc:
            logger.warning("get_pr_by_branch failed for branch %r: %s", branch, exc)
        return None

    def detect_pr_refs(self, text: str) -> list[int]:
        """Scan *text* for PR/MR references and return a deduplicated list of numbers.

        Handles GitHub PR #N, GitLab MR !N, Bitbucket PR-N,
        and full URL patterns.
        """
        found: set[int] = set()
        patterns = self._patterns_for_provider()
        for pattern in patterns:
            for match in pattern.findall(text):
                try:
                    num = int(match)
                    if num > 0:
                        found.add(num)
                except ValueError:
                    pass
        return sorted(found)

    def fetch_prs_for_issue(
        self,
        issue_text: str,
        comment_texts: Optional[list[str]] = None,
        repo: Optional[str] = None,
        max_prs: int = 3,
    ) -> list[GitPRMetadata]:
        """High-level helper: detect PR refs in issue text + comments, fetch each.

        Returns up to *max_prs* GitPRMetadata objects (most recent first).
        Returns empty list when client is not enabled or no refs found.
        """
        if not self.enabled:
            return []

        all_text = issue_text or ""
        for c in (comment_texts or []):
            all_text += " " + c

        pr_numbers = self.detect_pr_refs(all_text)
        if not pr_numbers:
            return []

        results: list[GitPRMetadata] = []
        for num in pr_numbers[:max_prs]:
            try:
                pr = self.get_pr(num, repo=repo)
                results.append(pr)
            except Exception as exc:
                logger.warning("Failed to fetch PR #%d: %s", num, exc)

        return results

    # GitHub implementation
    def _get_github_pr(self, pr_number: int, repo: str) -> GitPRMetadata:
        owner, name = self._split_repo(repo)
        url = f"{self._base_url}/repos/{owner}/{name}/pulls/{pr_number}"
        data = self._get(url)
        return self._parse_github_pr(data, repo)

    @staticmethod
    def _parse_github_pr(data: dict, repo: str) -> GitPRMetadata:
        merged = bool(data.get("merged") or data.get("merged_at"))
        state = "merged" if merged else data.get("state", "open")
        desc = (data.get("body") or "")[:500]
        return GitPRMetadata(
            pr_number=data["number"],
            pr_title=data.get("title", ""),
            pr_url=data.get("html_url", ""),
            repo=repo,
            author=(data.get("user") or {}).get("login", "unknown"),
            state=state,
            merged=merged,
            merge_commit_sha=(data.get("merge_commit_sha") or "")[:12] or None,
            head_branch=(data.get("head") or {}).get("ref", ""),
            base_branch=(data.get("base") or {}).get("ref", ""),
            created_at=data.get("created_at"),
            merged_at=data.get("merged_at"),
            description=desc or None,
            provider="github",
        )

    # GitLab implementation

    def _get_gitlab_mr(self, mr_number: int, repo: str) -> GitPRMetadata:
        # GitLab: project path encoded as URL component
        encoded = repo.replace("/", "%2F")
        url = f"{self._base_url}/api/v4/projects/{encoded}/merge_requests/{mr_number}"
        data = self._get(url)
        merged = data.get("state") == "merged"
        desc = (data.get("description") or "")[:500]
        return GitPRMetadata(
            pr_number=data["iid"],
            pr_title=data.get("title", ""),
            pr_url=data.get("web_url", ""),
            repo=repo,
            author=(data.get("author") or {}).get("username", "unknown"),
            state=data.get("state", "open"),
            merged=merged,
            merge_commit_sha=(data.get("merge_commit_sha") or "")[:12] or None,
            head_branch=data.get("source_branch", ""),
            base_branch=data.get("target_branch", ""),
            created_at=data.get("created_at"),
            merged_at=data.get("merged_at"),
            description=desc or None,
            provider="gitlab",
        )

    # Bitbucket implementation

    def _get_bitbucket_pr(self, pr_number: int, repo: str) -> GitPRMetadata:
        owner, name = self._split_repo(repo)
        url = f"{self._base_url}/2.0/repositories/{owner}/{name}/pullrequests/{pr_number}"
        data = self._get(url)
        state_raw = data.get("state", "OPEN").upper()
        merged = state_raw == "MERGED"
        state = "merged" if merged else state_raw.lower()
        desc = (data.get("description") or "")[:500]
        merge_sha = (
            (data.get("merge_commit") or {}).get("hash", "")[:12] or None
        )
        return GitPRMetadata(
            pr_number=data["id"],
            pr_title=data.get("title", ""),
            pr_url=(data.get("links") or {}).get("html", {}).get("href", ""),
            repo=repo,
            author=(data.get("author") or {}).get("display_name", "unknown"),
            state=state,
            merged=merged,
            merge_commit_sha=merge_sha,
            head_branch=(data.get("source") or {}).get("branch", {}).get("name", ""),
            base_branch=(data.get("destination") or {}).get("branch", {}).get("name", ""),
            created_at=data.get("created_on"),
            merged_at=data.get("updated_on") if merged else None,
            description=desc or None,
            provider="bitbucket",
        )

    # HTTP helpers

    def _get(self, url: str, params: Optional[dict] = None) -> dict | list:
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            logger.error("Git API error %s for %s: %s", status, url, exc)
            raise
        except Timeout:
            logger.error("Git API request timed out: %s", url)
            raise

    # Utilities

    def _default_base_url(self) -> str:
        defaults = {
            "github": "https://api.github.com",
            "gitlab": "https://gitlab.com",
            "bitbucket": "https://api.bitbucket.org",
        }
        return defaults.get(self._provider, "https://api.github.com")

    def _resolve_repo(self, repo: Optional[str]) -> str:
        """Return owner/repo string, falling back to configured defaults."""
        if repo:
            return repo if "/" in repo else f"{self._owner}/{repo}"
        if self._repo:
            return self._repo if "/" in self._repo else f"{self._owner}/{self._repo}"
        raise ValueError(
            "No repository specified. Pass repo= or set GIT_OWNER + GIT_REPO env vars."
        )

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        """Split 'owner/name' into (owner, name)."""
        parts = repo.split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"Expected 'owner/repo' format, got: {repo!r}")
        return parts[0], parts[1]

    def _patterns_for_provider(self) -> list[re.Pattern]:
        if self._provider == "gitlab":
            return _GITLAB_MR_PATTERNS
        if self._provider == "bitbucket":
            return _BITBUCKET_PR_PATTERNS
        return _GITHUB_PR_PATTERNS  # default
