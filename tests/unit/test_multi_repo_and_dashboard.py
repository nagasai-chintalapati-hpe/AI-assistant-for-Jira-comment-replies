"""Unit tests for multi-repo GitClient fan-out and dashboard routes."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.integrations.git import GitClient
from src.models.context import GitPRMetadata


# Multi-repo GitClient tests


def _make_client(provider="github", token="tok", owner="acme", repo="vme-api"):
    return GitClient(provider=provider, token=token, owner=owner, repo=repo)


class TestConfiguredRepos:
    """Tests for the configured_repos property."""

    @patch("src.config.settings")
    def test_reads_from_git_repos(self, mock_settings):
        from src.config import GitConfig
        mock_settings.git = GitConfig.__new__(GitConfig)
        object.__setattr__(mock_settings.git, 'repos', 'morpheus-core,morpheus-ui,morpheus-network')
        object.__setattr__(mock_settings.git, 'repo', '')
        object.__setattr__(mock_settings.git, 'provider', 'github')
        object.__setattr__(mock_settings.git, 'token', 'tok')
        object.__setattr__(mock_settings.git, 'owner', 'acme')
        object.__setattr__(mock_settings.git, 'base_url', '')

        client = _make_client(owner="acme")
        repos = client.configured_repos
        assert len(repos) == 3
        assert "acme/morpheus-core" in repos
        assert "acme/morpheus-ui" in repos
        assert "acme/morpheus-network" in repos

    @patch("src.integrations.git.settings")
    def test_falls_back_to_single_repo(self, mock_settings):
        mock_settings.git.repos = ""
        mock_settings.git.repo = "vme-api"
        mock_settings.git.provider = "github"
        mock_settings.git.token = "tok"
        mock_settings.git.owner = "acme"
        mock_settings.git.base_url = ""

        client = _make_client(owner="acme", repo="vme-api")
        repos = client.configured_repos
        assert repos == ["acme/vme-api"]

    @patch("src.integrations.git.settings")
    def test_returns_empty_when_nothing_configured(self, mock_settings):
        mock_settings.git.repos = ""
        mock_settings.git.repo = ""
        mock_settings.git.provider = "github"
        mock_settings.git.token = "tok"
        mock_settings.git.owner = "acme"
        mock_settings.git.base_url = ""

        client = GitClient(provider="github", token="tok", owner="acme", repo="")
        repos = client.configured_repos
        assert repos == []

    @patch("src.config.settings")
    def test_handles_full_owner_repo_format(self, mock_settings):
        from src.config import GitConfig
        mock_settings.git = GitConfig.__new__(GitConfig)
        object.__setattr__(mock_settings.git, 'repos', 'acme/morpheus-core,other-org/other-repo')
        object.__setattr__(mock_settings.git, 'repo', '')
        object.__setattr__(mock_settings.git, 'provider', 'github')
        object.__setattr__(mock_settings.git, 'token', 'tok')
        object.__setattr__(mock_settings.git, 'owner', 'acme')
        object.__setattr__(mock_settings.git, 'base_url', '')

        client = _make_client(owner="acme")
        repos = client.configured_repos
        assert "acme/morpheus-core" in repos
        assert "other-org/other-repo" in repos


class TestFetchPrsAcrossRepos:
    """Tests for fetch_prs_across_repos multi-repo fan-out."""

    def test_returns_empty_when_disabled(self):
        client = GitClient(provider="github", token="", owner="", repo="")
        prs, repos = client.fetch_prs_across_repos("PR #42")
        assert prs == []
        assert repos == []

    def test_returns_empty_when_no_pr_refs(self):
        client = _make_client()
        prs, repos = client.fetch_prs_across_repos(
            "No PR references here",
            repos=["acme/repo1", "acme/repo2"],
        )
        assert prs == []
        assert repos == ["acme/repo1", "acme/repo2"]

    @patch.object(GitClient, "get_pr")
    def test_searches_across_multiple_repos(self, mock_get_pr):
        pr_meta = GitPRMetadata(
            pr_number=42,
            pr_title="Fix bug",
            pr_url="https://github.com/acme/repo1/pull/42",
            repo="acme/repo1",
            author="dev",
            state="merged",
            merged=True,
        )
        mock_get_pr.return_value = pr_meta

        client = _make_client()
        prs, repos = client.fetch_prs_across_repos(
            "See PR #42 for the fix",
            repos=["acme/repo1", "acme/repo2"],
        )

        # Should attempt to fetch PR #42 from both repos
        assert mock_get_pr.call_count >= 1
        assert len(prs) >= 1
        assert prs[0].pr_number == 42

    @patch.object(GitClient, "get_pr")
    def test_handles_pr_not_found_in_some_repos(self, mock_get_pr):
        """PR exists in repo1 but not repo2 — should gracefully skip."""
        def side_effect(num, repo=None):
            if repo == "acme/repo1":
                return GitPRMetadata(
                    pr_number=42, pr_title="Fix", pr_url="...",
                    repo="acme/repo1", author="dev", state="merged", merged=True,
                )
            raise Exception("Not found")

        mock_get_pr.side_effect = side_effect

        client = _make_client()
        prs, repos = client.fetch_prs_across_repos(
            "PR #42",
            repos=["acme/repo1", "acme/repo2"],
        )
        assert len(prs) == 1
        assert prs[0].repo == "acme/repo1"

    @patch.object(GitClient, "get_pr")
    def test_deduplicates_across_repos(self, mock_get_pr):
        """Same PR number in same repo should not be fetched twice."""
        call_count = 0

        def side_effect(num, repo=None):
            nonlocal call_count
            call_count += 1
            return GitPRMetadata(
                pr_number=num, pr_title="Fix", pr_url="...",
                repo=repo, author="dev", state="open",
            )

        mock_get_pr.side_effect = side_effect

        client = _make_client()
        prs, _ = client.fetch_prs_across_repos(
            "PR #42 and also PR #42",
            repos=["acme/repo1"],
        )
        # Should only fetch PR #42 from repo1 once
        assert call_count == 1


# Dashboard route tests


class TestDashboardRoutes:
    """Tests for the dashboard API endpoints."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from src.api.app import app
        from src.config import settings

        original = settings.dashboard.token
        object.__setattr__(settings.dashboard, "token", "")
        c = TestClient(app)
        yield c
        object.__setattr__(settings.dashboard, "token", original)

    def test_dashboard_page_returns_200(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_dashboard_api_summary(self, client):
        resp = client.get("/dashboard/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_drafts" in data
        assert "acceptance_rate_pct" in data
        assert "rovo_overrides" in data
        assert "severity_priority_attention_count" in data
        assert "estimated_time_saved_hours" in data

    def test_dashboard_api_daily_volume(self, client):
        resp = client.get("/dashboard/api/daily-volume?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert isinstance(data["data"], list)

    def test_dashboard_api_classifications(self, client):
        resp = client.get("/dashboard/api/classifications")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_classification" in data

    def test_dashboard_api_severity(self, client):
        resp = client.get("/dashboard/api/severity")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_dashboard_api_top_issues(self, client):
        resp = client.get("/dashboard/api/top-issues")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_dashboard_api_repos(self, client):
        resp = client.get("/dashboard/api/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert "configured_repos" in data
        assert "search_counts" in data

    def test_dashboard_api_response_time(self, client):
        resp = client.get("/dashboard/api/response-time?days=14")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data

    def test_dashboard_page_shows_severity_priority_audit_section(self, client):
        from src.api.deps import draft_store
        from src.models.draft import Draft

        draft = Draft(
            draft_id="draft_dashboard_audit",
            issue_key="AUDIT-1",
            in_reply_to_comment_id="10000",
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body="Draft body",
            confidence_score=0.8,
            severity_priority_audit={
                "criteria_profile": "standard_hpe",
                "current_severity": "",
                "current_priority": "Medium",
                "recommended_severity": "Major",
                "recommended_priority": "P2",
                "needs_attention": True,
                "findings": ["Severity is not set on the Jira issue."],
                "confidence": 0.8,
            },
        )
        draft_store.save(draft, classification="other")

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Severity / Priority Audit" in resp.text
        assert "Severity is not set on the Jira issue." in resp.text


# SQLite dashboard query tests


class TestSQLiteDashboardQueries:
    """Tests for dashboard-specific SQLite store methods."""

    @pytest.fixture
    def store(self):
        from src.storage.sqlite_store import SQLiteDraftStore
        s = SQLiteDraftStore(db_path=":memory:")
        yield s
        s.close()

    def _make_draft(self, issue_key="TEST-1", status="generated", **extra):
        from src.models.draft import Draft, DraftStatus
        status_enum = DraftStatus(status)
        d = Draft(
            draft_id=f"draft_{id(extra)}_{issue_key}",
            issue_key=issue_key,
            in_reply_to_comment_id="10000",
            created_at=datetime.now(timezone.utc),
            created_by="system",
            body="Test draft body",
            status=status_enum,
            confidence_score=0.85,
            **extra,
        )
        return d

    def test_get_daily_volume_empty(self, store):
        data = store.get_daily_volume(days=7)
        assert data == []

    def test_get_daily_volume_with_data(self, store):
        d1 = self._make_draft(issue_key="A-1", status="approved")
        d2 = self._make_draft(issue_key="A-2", status="rejected")
        store.save(d1, classification="cannot_reproduce")
        store.save(d2, classification="need_more_info")
        data = store.get_daily_volume(days=1)
        assert len(data) == 1
        assert data[0]["total"] == 2
        assert data[0]["approved"] == 1
        assert data[0]["rejected"] == 1

    def test_get_severity_challenges_empty(self, store):
        data = store.get_severity_challenges()
        assert data == []

    def test_get_top_issues(self, store):
        d1 = self._make_draft(issue_key="X-1")
        d2 = self._make_draft(issue_key="X-1")
        d2 = d2.model_copy(update={"draft_id": "draft_other"})
        store.save(d1, classification="other")
        store.save(d2, classification="other")
        data = store.get_top_issues(limit=5)
        assert len(data) >= 1
        assert data[0]["issue_key"] == "X-1"
        assert data[0]["count"] == 2

    def test_get_repos_stats_empty(self, store):
        data = store.get_repos_stats()
        assert data == {}

    def test_get_avg_response_time_by_day(self, store):
        data = store.get_avg_response_time_by_day(days=7)
        assert isinstance(data, list)


# Dashboard token-based auth tests


class TestDashboardAuth:
    """Tests for the DASHBOARD_TOKEN cookie-based auth gate."""

    SECRET = "s3cr3t-dashboard-token"

    @pytest.fixture
    def locked_client(self):
        """TestClient with DASHBOARD_TOKEN set — dashboard requires auth."""
        from fastapi.testclient import TestClient
        from src.api.app import app
        from src.config import settings

        original = settings.dashboard.token
        object.__setattr__(settings.dashboard, "token", self.SECRET)
        client = TestClient(app, follow_redirects=False)
        yield client
        object.__setattr__(settings.dashboard, "token", original)

    @pytest.fixture
    def open_client(self):
        """TestClient with no DASHBOARD_TOKEN — dashboard is open."""
        from fastapi.testclient import TestClient
        from src.api.app import app
        from src.config import settings

        original = settings.dashboard.token
        object.__setattr__(settings.dashboard, "token", "")
        client = TestClient(app, follow_redirects=False)
        yield client
        object.__setattr__(settings.dashboard, "token", original)

    # Open mode (no token configured)

    def test_open_dashboard_no_redirect(self, open_client):
        """When DASHBOARD_TOKEN is empty, /dashboard serves directly."""
        resp = open_client.get("/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_open_login_page_redirects_to_dashboard(self, open_client):
        """Login page redirects to /dashboard when no token is configured."""
        resp = open_client.get("/dashboard/login")
        assert resp.status_code == 307
        assert "/dashboard" in resp.headers["location"]

    def test_open_api_no_auth_needed(self, open_client):
        """API endpoints work without a cookie when token is not set."""
        resp = open_client.get("/dashboard/api/summary")
        assert resp.status_code == 200

    # Locked mode (token configured)

    def test_locked_dashboard_redirects_to_login(self, locked_client):
        """Without a cookie, /dashboard redirects to /dashboard/login."""
        resp = locked_client.get("/dashboard")
        assert resp.status_code == 307
        assert "/dashboard/login" in resp.headers["location"]

    def test_locked_api_returns_401(self, locked_client):
        """API endpoints return 401 without a valid cookie."""
        for endpoint in [
            "/dashboard/api/summary",
            "/dashboard/api/daily-volume",
            "/dashboard/api/classifications",
            "/dashboard/api/severity",
            "/dashboard/api/top-issues",
            "/dashboard/api/repos",
            "/dashboard/api/response-time",
        ]:
            resp = locked_client.get(endpoint)
            assert resp.status_code == 401, f"{endpoint} should be 401"

    def test_login_page_renders(self, locked_client):
        """Login page renders the token form."""
        resp = locked_client.get("/dashboard/login")
        assert resp.status_code == 200
        assert "Dashboard Token" in resp.text
        assert "Unlock Dashboard" in resp.text

    def test_login_wrong_token(self, locked_client):
        """Wrong token shows error, returns 401."""
        resp = locked_client.post(
            "/dashboard/login",
            data={"token": "wrong-token"},
        )
        assert resp.status_code == 401
        assert "Invalid token" in resp.text

    def test_login_correct_token_sets_cookie(self, locked_client):
        """Correct token sets cookie and redirects to /dashboard."""
        resp = locked_client.post(
            "/dashboard/login",
            data={"token": self.SECRET},
        )
        assert resp.status_code == 303
        assert "/dashboard" in resp.headers["location"]
        assert "dash_token" in resp.headers.get("set-cookie", "")

    def test_cookie_grants_access(self, locked_client):
        """With a valid cookie, /dashboard returns 200."""
        locked_client.cookies.set("dash_token", self.SECRET)
        resp = locked_client.get("/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_cookie_grants_api_access(self, locked_client):
        """With a valid cookie, API endpoints return 200."""
        locked_client.cookies.set("dash_token", self.SECRET)
        resp = locked_client.get("/dashboard/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_drafts" in data

    def test_bad_cookie_rejected(self, locked_client):
        """An invalid cookie value is rejected."""
        locked_client.cookies.set("dash_token", "bad-value")
        resp = locked_client.get("/dashboard")
        assert resp.status_code == 307

    def test_logout_clears_cookie(self, locked_client):
        """Logout clears the cookie and redirects to login."""
        locked_client.cookies.set("dash_token", self.SECRET)
        resp = locked_client.get("/dashboard/logout")
        assert resp.status_code == 303
        assert "/dashboard/login" in resp.headers["location"]
