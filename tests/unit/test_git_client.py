"""Unit tests for GitClient — GitHub, GitLab, Bitbucket."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.integrations.git import GitClient
from src.models.context import GitPRMetadata


# Fixtures

GITHUB_PR_PAYLOAD = {
    "number": 42,
    "title": "Fix snapshot lock timeout",
    "html_url": "https://github.com/acme/vme-api/pull/42",
    "state": "closed",
    "merged": True,
    "merged_at": "2026-02-10T09:00:00Z",
    "created_at": "2026-02-09T12:00:00Z",
    "merge_commit_sha": "abc123def456",
    "body": "Fixes DEFECT-123. Adds lock timeout metrics.",
    "user": {"login": "dev-alice"},
    "head": {"ref": "fix/snapshot-lock"},
    "base": {"ref": "main"},
}

GITLAB_MR_PAYLOAD = {
    "iid": 15,
    "title": "Fix API 500 on snapshot",
    "web_url": "https://gitlab.com/acme/vme/merge_requests/15",
    "state": "merged",
    "merged_at": "2026-02-11T10:00:00Z",
    "created_at": "2026-02-10T08:00:00Z",
    "merge_commit_sha": "deadbeef1234",
    "description": "Closes #99. Rate limiter patch.",
    "author": {"username": "dev-bob"},
    "source_branch": "hotfix/snapshot",
    "target_branch": "main",
}

BITBUCKET_PR_PAYLOAD = {
    "id": 7,
    "title": "DEFECT-321 snapshot fix",
    "state": "MERGED",
    "created_on": "2026-02-08T07:00:00Z",
    "updated_on": "2026-02-09T09:00:00Z",
    "description": "See DEFECT-321 for details.",
    "links": {"html": {"href": "https://bitbucket.org/acme/vme/pull-requests/7"}},
    "author": {"display_name": "dev-charlie"},
    "merge_commit": {"hash": "cafebabe9988"},
    "source": {"branch": {"name": "feature/snapshot-fix"}},
    "destination": {"branch": {"name": "main"}},
}


def _make_client(provider="github", token="tok", owner="acme", repo="vme-api"):
    return GitClient(provider=provider, token=token, owner=owner, repo=repo)


# enabled property

def test_enabled_when_token_and_owner_set():
    client = _make_client()
    assert client.enabled is True


def test_disabled_when_no_token():
    client = GitClient(provider="github", token="", owner="acme", repo="vme-api")
    assert client.enabled is False


def test_disabled_when_no_owner():
    client = GitClient(provider="github", token="tok", owner="", repo="vme-api")
    assert client.enabled is False


# detect_pr_refs

@pytest.mark.parametrize("text,expected", [
    ("See PR #42 for the fix", [42]),
    ("Merged PR #100 and PR #200", [100, 200]),
    ("https://github.com/acme/vme/pull/99 was merged", [99]),
    ("No PR references here", []),
    ("PR #0 is invalid", []),               # 0 is filtered
    ("Duplicate PR #42 and #42", [42]),     # deduplication
])
def test_detect_pr_refs_github(text, expected):
    client = _make_client(provider="github")
    assert client.detect_pr_refs(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("MR !15 addresses the issue", [15]),
    ("https://gitlab.com/acme/vme/-/merge_requests/15", [15]),
    ("No refs", []),
])
def test_detect_pr_refs_gitlab(text, expected):
    client = _make_client(provider="gitlab")
    assert client.detect_pr_refs(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Fixed in PR-7", [7]),
    ("https://bitbucket.org/acme/vme/pull-requests/7", [7]),
    ("Nothing", []),
])
def test_detect_pr_refs_bitbucket(text, expected):
    client = _make_client(provider="bitbucket")
    assert client.detect_pr_refs(text) == expected


# get_pr GitHub

def test_get_github_pr_success():
    client = _make_client(provider="github")
    mock_resp = MagicMock()
    mock_resp.json.return_value = GITHUB_PR_PAYLOAD
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr(42)

    assert isinstance(pr, GitPRMetadata)
    assert pr.pr_number == 42
    assert pr.pr_title == "Fix snapshot lock timeout"
    assert pr.state == "merged"
    assert pr.merged is True
    assert pr.merge_commit_sha == "abc123def456"  # 12 chars (already exactly 12)
    assert pr.head_branch == "fix/snapshot-lock"
    assert pr.base_branch == "main"
    assert pr.author == "dev-alice"
    assert pr.provider == "github"
    assert "Fixes DEFECT-123" in pr.description


def test_get_github_pr_sha_truncated():
    """merge_commit_sha should be truncated to 12 chars."""
    client = _make_client(provider="github")
    payload = dict(GITHUB_PR_PAYLOAD)
    payload["merge_commit_sha"] = "abcdef1234567890"
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr(42)

    assert len(pr.merge_commit_sha) == 12


def test_get_github_pr_open_not_merged():
    client = _make_client(provider="github")
    payload = dict(GITHUB_PR_PAYLOAD)
    payload["merged"] = False
    payload["merged_at"] = None
    payload["state"] = "open"
    payload["merge_commit_sha"] = None
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr(42)

    assert pr.state == "open"
    assert pr.merged is False
    assert pr.merge_commit_sha is None


# get_pr GitLab

def test_get_gitlab_mr_success():
    client = _make_client(provider="gitlab", owner="acme", repo="vme")
    mock_resp = MagicMock()
    mock_resp.json.return_value = GITLAB_MR_PAYLOAD
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr(15)

    assert pr.pr_number == 15
    assert pr.state == "merged"
    assert pr.merged is True
    assert pr.author == "dev-bob"
    assert pr.head_branch == "hotfix/snapshot"
    assert pr.provider == "gitlab"


# get_pr Bitbucket

def test_get_bitbucket_pr_success():
    client = _make_client(provider="bitbucket", owner="acme", repo="vme")
    mock_resp = MagicMock()
    mock_resp.json.return_value = BITBUCKET_PR_PAYLOAD
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr(7)

    assert pr.pr_number == 7
    assert pr.state == "merged"
    assert pr.merged is True
    assert pr.author == "dev-charlie"
    assert pr.head_branch == "feature/snapshot-fix"
    assert pr.provider == "bitbucket"


# get_pr_by_branch

def test_get_pr_by_branch_found():
    client = _make_client(provider="github")
    mock_resp = MagicMock()
    mock_resp.json.return_value = [GITHUB_PR_PAYLOAD]
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr_by_branch("fix/snapshot-lock")

    assert pr is not None
    assert pr.pr_number == 42


def test_get_pr_by_branch_not_found():
    client = _make_client(provider="github")
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        pr = client.get_pr_by_branch("no-such-branch")

    assert pr is None


def test_get_pr_by_branch_not_supported_for_gitlab():
    """GitLab provider should skip get_pr_by_branch and return None."""
    client = _make_client(provider="gitlab")
    result = client.get_pr_by_branch("some-branch")
    assert result is None


# fetch_prs_for_issue

def test_fetch_prs_for_issue_detects_and_fetches():
    client = _make_client(provider="github")
    mock_resp = MagicMock()
    mock_resp.json.return_value = GITHUB_PR_PAYLOAD
    mock_resp.raise_for_status.return_value = None

    with patch.object(client._session, "get", return_value=mock_resp):
        prs = client.fetch_prs_for_issue(
            issue_text="Fix tracked in PR #42",
            comment_texts=["See also PR #42"],
        )

    assert len(prs) == 1
    assert prs[0].pr_number == 42


def test_fetch_prs_for_issue_no_refs():
    client = _make_client(provider="github")
    prs = client.fetch_prs_for_issue(issue_text="No PR refs here")
    assert prs == []


def test_fetch_prs_for_issue_disabled_when_no_token():
    client = GitClient(provider="github", token="", owner="acme", repo="vme-api")
    prs = client.fetch_prs_for_issue(issue_text="PR #42")
    assert prs == []


def test_fetch_prs_for_issue_respects_max_prs():
    client = _make_client(provider="github")
    mock_resp = MagicMock()
    mock_resp.json.return_value = GITHUB_PR_PAYLOAD
    mock_resp.raise_for_status.return_value = None

    # 5 distinct PR refs but max_prs=2
    with patch.object(client._session, "get", return_value=mock_resp):
        prs = client.fetch_prs_for_issue(
            issue_text="PR #1 PR #2 PR #3 PR #4 PR #5",
            max_prs=2,
        )

    assert len(prs) == 2


# HTTP error propagation

def test_get_pr_raises_on_http_error():
    from requests.exceptions import HTTPError
    from unittest.mock import MagicMock

    client = _make_client(provider="github")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = HTTPError(
        response=MagicMock(status_code=404)
    )

    with patch.object(client._session, "get", return_value=mock_resp):
        with pytest.raises(HTTPError):
            client.get_pr(999)


# _resolve_repo

def test_resolve_repo_uses_default():
    client = GitClient(provider="github", token="tok", owner="myorg", repo="myrepo")
    assert client._resolve_repo(None) == "myorg/myrepo"


def test_resolve_repo_explicit_full():
    client = GitClient(provider="github", token="tok", owner="myorg", repo="myrepo")
    assert client._resolve_repo("other/repo") == "other/repo"


def test_resolve_repo_explicit_short():
    client = GitClient(provider="github", token="tok", owner="myorg", repo="myrepo")
    assert client._resolve_repo("custom-repo") == "myorg/custom-repo"


def test_resolve_repo_raises_when_no_repo_configured():
    client = GitClient(provider="github", token="tok", owner="myorg", repo="")
    with pytest.raises(ValueError, match="No repository specified"):
        client._resolve_repo(None)


# unsupported provider

def test_unsupported_provider_raises():
    client = GitClient(provider="svn", token="tok", owner="acme", repo="vme")
    with pytest.raises(ValueError, match="Unsupported Git provider"):
        client.get_pr(1)
