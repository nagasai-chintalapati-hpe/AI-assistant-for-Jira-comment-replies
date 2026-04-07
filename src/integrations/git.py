"""Git provider client — GitHub, GitLab, Bitbucket PR metadata."""

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

    @property
    def configured_repos(self) -> list[str]:
        """Return all configured repos as owner/name strings.

        Reads from ``GIT_REPOS`` (comma-separated) first, falls back
        to the single ``GIT_REPO``.  Returns an empty list when nothing
        is configured.
        """
        from src.config import settings as _s

        repos: list[str] = []
        raw = _s.git.repos
        if raw:
            for r in raw.split(","):
                r = r.strip()
                if not r:
                    continue
                repos.append(r if "/" in r else f"{self._owner}/{r}")
        if not repos and self._repo:
            repos.append(
                self._repo if "/" in self._repo else f"{self._owner}/{self._repo}"
            )
        return repos

    # Public API

    def get_pr(
        self,
        pr_number: int,
        repo: Optional[str] = None,
    ) -> GitPRMetadata:
        """Fetch a PR/MR by number."""
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
        """Find an open PR for a branch. GitHub only."""
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
        """Scan text for PR/MR references and return deduplicated numbers."""
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

    def search_prs_by_issue_key(
        self,
        issue_key: str,
        repo: Optional[str] = None,
        max_prs: int = 3,
    ) -> list[GitPRMetadata]:
        """Search for PRs whose branch name or title contains the Jira issue key.

        This catches the common convention where developers name branches
        like ``feature/IP-15-fix-crash`` or PR titles like ``IP-15: Fix crash``.

        When *repo* is ``None`` and no ``GIT_REPO`` is configured the search
        runs across the entire GitHub owner/org.
        """
        if not self.enabled or not issue_key:
            return []

        try:
            resolved_repo = self._resolve_repo(repo)
        except ValueError:
            resolved_repo = None

        if self._provider == "github":
            return self._search_github_prs_by_key(issue_key, resolved_repo, max_prs)
        if self._provider == "gitlab":
            if not resolved_repo:
                return []
            return self._search_gitlab_mrs_by_key(issue_key, resolved_repo, max_prs)
        # Bitbucket Cloud doesn't have a search-by-branch PR API — skip
        return []

    def _search_github_prs_by_key(
        self, issue_key: str, repo: Optional[str], max_prs: int,
    ) -> list[GitPRMetadata]:
        """GitHub: use the search/issues endpoint to find PRs mentioning the key.

        Builds a cascade of search scopes from most specific (single repo)
        to broadest (global, no scope qualifier).  The global fallback is
        essential for GitHub Enterprise / SAML-protected orgs where
        ``org:`` and ``user:`` qualifiers may 422.
        """
        url = f"{self._base_url}/search/issues"

        # Build scopes: most specific → broadest
        scopes: list[str] = []
        if repo:
            owner, name = self._split_repo(repo)
            scopes.append(f"repo:{owner}/{name}")
        else:
            owner = self._owner

        scopes.append(f"org:{owner}")    # works for GitHub orgs
        scopes.append(f"user:{owner}")   # works for personal accounts

        # Also search every org the token has access to
        for org_login in self._get_accessible_orgs():
            scope = f"org:{org_login}"
            if scope not in scopes:
                scopes.append(scope)

        # Final fallback: global search (no scope qualifier) — works even
        # when org/user scopes 422 due to SAML enforcement
        scopes.append("")

        results: list[GitPRMetadata] = []
        seen_keys: set[str] = set()

        for scope in scopes:
            if len(results) >= max_prs:
                break
            query = f"{issue_key} {scope} type:pr".strip()
            try:
                data = self._get(url, params={"q": query, "per_page": max_prs})
                for item in (data.get("items") or [])[:max_prs]:
                    pr_number = item.get("number")
                    # Extract repo from the PR's html_url
                    html_url = item.get("html_url", "")
                    pr_repo = self._repo_from_html_url(html_url) or repo
                    dedup_key = f"{pr_repo}#{pr_number}"
                    if not pr_number or dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    try:
                        pr = self.get_pr(pr_number, repo=pr_repo)
                        results.append(pr)
                    except Exception as exc:
                        logger.debug("Failed to fetch searched PR #%d: %s", pr_number, exc)
                # If we found results in this scope, no need to widen
                if results:
                    break
            except Exception as exc:
                logger.debug("GitHub PR search scope %r for %s failed: %s", scope, issue_key, exc)
                continue

        if not results:
            logger.info("No GitHub PRs found for %s across scopes %s", issue_key, scopes)
        return results[:max_prs]

    @staticmethod
    def _repo_from_html_url(html_url: str) -> Optional[str]:
        """Extract 'owner/repo' from a GitHub PR html_url."""
        m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/", html_url)
        return m.group(1) if m else None

    def _search_gitlab_mrs_by_key(
        self, issue_key: str, repo: str, max_prs: int,
    ) -> list[GitPRMetadata]:
        """GitLab: search MRs by title/branch containing the issue key."""
        encoded = repo.replace("/", "%2F")
        url = f"{self._base_url}/api/v4/projects/{encoded}/merge_requests"
        try:
            data = self._get(url, params={"search": issue_key, "per_page": max_prs})
            results: list[GitPRMetadata] = []
            for item in (data if isinstance(data, list) else [])[:max_prs]:
                try:
                    results.append(self._get_gitlab_mr(item["iid"], repo))
                except Exception:
                    pass
            return results
        except Exception as exc:
            logger.warning("GitLab MR search for %s failed: %s", issue_key, exc)
            return []

    def fetch_prs_for_issue(
        self,
        issue_text: str,
        comment_texts: Optional[list[str]] = None,
        repo: Optional[str] = None,
        max_prs: int = 3,
    ) -> list[GitPRMetadata]:
        """Detect PR refs in issue text + comments and fetch each."""
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

    def fetch_prs_across_repos(
        self,
        issue_text: str,
        comment_texts: Optional[list[str]] = None,
        repos: Optional[list[str]] = None,
        max_prs_per_repo: int = 3,
        issue_key: Optional[str] = None,
    ) -> tuple[list[GitPRMetadata], list[str]]:
        """Fan out PR search across multiple repositories.

        First tries explicit PR references in issue text/comments.
        Falls back to searching by issue key in branch names and titles.
        """
        if not self.enabled:
            return [], []

        target_repos = repos or self.configured_repos
        if not target_repos:
            # No specific repos configured — do an org/user-wide search
            prs: list[GitPRMetadata] = []
            if issue_key:
                # search_prs_by_issue_key handles repo=None by doing
                # org/user-wide search across ALL repos
                prs = self.search_prs_by_issue_key(
                    issue_key, repo=None, max_prs=max_prs_per_repo,
                )
            repos_label = f"{self._owner}/*"
            return prs, [repos_label]

        all_text = issue_text or ""
        for c in (comment_texts or []):
            all_text += " " + c

        pr_numbers = self.detect_pr_refs(all_text)

        all_prs: list[GitPRMetadata] = []
        seen_keys: set[str] = set()  # "repo#number" dedup

        if pr_numbers:
            # Explicit PR references found — fetch them from each repo
            for repo in target_repos:
                for num in pr_numbers[:max_prs_per_repo]:
                    key = f"{repo}#{num}"
                    if key in seen_keys:
                        continue
                    try:
                        pr = self.get_pr(num, repo=repo)
                        all_prs.append(pr)
                        seen_keys.add(key)
                    except Exception as exc:
                        # PR doesn't exist in this repo — expected
                        logger.debug(
                            "PR #%d not found in %s: %s", num, repo, exc
                        )

        # Fallback: search by Jira issue key in branch/title
        if not all_prs and issue_key:
            for repo in target_repos:
                try:
                    found = self.search_prs_by_issue_key(
                        issue_key, repo=repo, max_prs=max_prs_per_repo,
                    )
                    for pr in found:
                        key = f"{pr.repo}#{pr.pr_number}"
                        if key not in seen_keys:
                            all_prs.append(pr)
                            seen_keys.add(key)
                except Exception as exc:
                    logger.debug(
                        "Issue-key PR search failed for %s in %s: %s",
                        issue_key, repo, exc,
                    )

        logger.info(
            "Multi-repo PR search: %d PRs found across %d repos",
            len(all_prs),
            len(target_repos),
        )
        return all_prs, target_repos

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

    def _get_accessible_orgs(self) -> list[str]:
        """Return org logins the authenticated token has access to (GitHub only).

        Results are cached after the first call to avoid repeated API calls.
        """
        if self._provider != "github":
            return []
        if hasattr(self, "_cached_orgs"):
            return self._cached_orgs  # type: ignore[has-type]
        try:
            orgs_data = self._get(f"{self._base_url}/user/orgs")
            self._cached_orgs: list[str] = [
                o["login"] for o in (orgs_data if isinstance(orgs_data, list) else [])
                if o.get("login")
            ]
            logger.info("GitHub accessible orgs: %s", self._cached_orgs)
            return self._cached_orgs
        except Exception as exc:
            logger.debug("Failed to list GitHub orgs: %s", exc)
            self._cached_orgs = []
            return []

    def _patterns_for_provider(self) -> list[re.Pattern]:
        if self._provider == "gitlab":
            return _GITLAB_MR_PATTERNS
        if self._provider == "bitbucket":
            return _BITBUCKET_PR_PATTERNS
        return _GITHUB_PR_PATTERNS  # default
