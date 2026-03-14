"""TestRail API v2 client — runs, tests, and results.

Auth: API key (production) → session cookie fallback (SSO).
Set TESTRAIL_SESSION_COOKIE to the ``tr_session`` browser cookie value.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from requests.exceptions import HTTPError

from src.config import settings

logger = logging.getLogger(__name__)

# TestRail status ID → human-readable name
STATUS_MAP: dict[int, str] = {
    1: "passed",
    2: "blocked",
    3: "untested",
    4: "retest",
    5: "failed",
}


class TestRailClient:
    """TestRail API v2 client with API-key / session-cookie auth."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_key: Optional[str] = None,
        session_cookie: Optional[str] = None,
    ) -> None:
        self._base_url = (
            base_url or settings.testrail.base_url
        ).rstrip("/")
        self._username = username or settings.testrail.username
        self._api_key = api_key or settings.testrail.api_key
        self._session_cookie = session_cookie or settings.testrail.session_cookie

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        # Choose auth mode: API key first, session cookie as fallback
        if self._api_key and self._username:
            self._session.auth = (self._username, self._api_key)
            self._auth_mode = "api_key"
        elif self._session_cookie:
            self._session.cookies.set(
                "tr_session", self._session_cookie,
                domain=self._base_url.replace("https://", "").replace("http://", ""),
            )
            self._auth_mode = "session_cookie"
        else:
            self._auth_mode = "none"

        if self.enabled:
            logger.info(
                "TestRail client ready (auth=%s, url=%s)",
                self._auth_mode, self._base_url,
            )

    @property
    def enabled(self) -> bool:
        """Whether any valid auth is configured."""
        if not self._base_url:
            return False
        if self._auth_mode == "api_key":
            return bool(self._username and self._api_key)
        if self._auth_mode == "session_cookie":
            return bool(self._session_cookie)
        return False

    @property
    def auth_mode(self) -> str:
        """Current authentication mode: 'api_key', 'session_cookie', or 'none'."""
        return self._auth_mode

    def ping(self) -> bool:
        """Return True if auth is valid, False if cookie/key is expired or wrong."""
        try:
            self._get("get_projects")
            return True
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                logger.warning(
                    "TestRail auth failed (401) — session cookie may have expired. "
                    "Refresh TESTRAIL_SESSION_COOKIE in .env"
                )
                return False
            raise

    # Helpers

    def _get(self, endpoint: str, params: Optional[dict] = None) -> Any:
        url = f"{self._base_url}/index.php?/api/v2/{endpoint}"
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # Runs

    def get_run(self, run_id: int) -> dict[str, Any]:
        """Return a single run dict by ID."""
        try:
            return self._get(f"get_run/{run_id}")
        except Exception as exc:
            logger.error("Failed to fetch TestRail run %d: %s", run_id, exc)
            raise

    def get_runs(
        self,
        project_id: Optional[int] = None,
        suite_id: Optional[int] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List recent runs (newest first). Falls back to config project/suite IDs."""
        pid = project_id or settings.testrail.project_id
        if not pid:
            raise ValueError("project_id must be provided or set via TESTRAIL_PROJECT_ID")
        sid = suite_id or settings.testrail.suite_id or None
        try:
            params: dict[str, Any] = {"limit": limit}
            if sid:
                params["suite_id"] = sid
            data = self._get(f"get_runs/{pid}", params=params)
            # API returns {"offset":..., "runs":[...]} or plain list
            if isinstance(data, dict):
                return data.get("runs", [])
            return data
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                raise
            logger.error("Failed to list TestRail runs: %s", exc)
            return []
        except Exception as exc:
            logger.error("Failed to list TestRail runs: %s", exc)
            return []

    # Tests

    def get_tests(
        self,
        run_id: int,
        status_id: Optional[str] = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """List tests in a run. status_id: comma-separated IDs e.g. '4,5' (retest+failed)."""
        try:
            params: dict[str, Any] = {"limit": limit}
            if status_id:
                params["status_id"] = status_id
            data = self._get(f"get_tests/{run_id}", params=params)
            if isinstance(data, dict):
                return data.get("tests", [])
            return data
        except Exception as exc:
            logger.error("Failed to list tests for run %d: %s", run_id, exc)
            return []

    def get_results_for_test(
        self,
        test_id: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return result history for a single test (newest first)."""
        try:
            data = self._get(
                f"get_results/{test_id}", params={"limit": limit}
            )
            if isinstance(data, dict):
                return data.get("results", [])
            return data
        except Exception as exc:
            logger.error("Failed to get results for test %d: %s", test_id, exc)
            return []

    def get_results_for_run(
        self,
        run_id: int,
        status_id: Optional[str] = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """Return all results for a run, optionally filtered by status ID."""
        try:
            params: dict[str, Any] = {"limit": limit}
            if status_id:
                params["status_id"] = status_id
            data = self._get(f"get_results_for_run/{run_id}", params=params)
            if isinstance(data, dict):
                return data.get("results", [])
            return data
        except Exception as exc:
            logger.error("Failed to get results for run %d: %s", run_id, exc)
            return []

    # Summaries

    def get_run_summary(self, run_id: int) -> dict[str, Any]:
        """Return a structured run summary dict for context enrichment."""
        run = self.get_run(run_id)

        passed = run.get("passed_count", 0) or 0
        failed = run.get("failed_count", 0) or 0
        blocked = run.get("blocked_count", 0) or 0
        retest = run.get("retest_count", 0) or 0
        untested = run.get("untested_count", 0) or 0
        total = passed + failed + blocked + retest + untested
        pass_rate = (passed / total * 100) if total > 0 else 0.0

        # Fetch failed + retest tests for detail
        failed_tests: list[dict[str, Any]] = []
        if failed + retest > 0:
            tests = self.get_tests(run_id, status_id="4,5", limit=50)
            for t in tests:
                sid = t.get("status_id", 0)
                failed_tests.append({
                    "title": t.get("title", ""),
                    "status": STATUS_MAP.get(sid, f"status_{sid}"),
                    "case_id": t.get("case_id"),
                    "test_id": t.get("id"),
                })

        return {
            "run_id": run_id,
            "name": run.get("name", ""),
            "url": run.get("url", f"{self._base_url}/index.php?/runs/view/{run_id}"),
            "passed": passed,
            "failed": failed,
            "blocked": blocked,
            "retest": retest,
            "untested": untested,
            "total": total,
            "pass_rate": round(pass_rate, 1),
            "failed_tests": failed_tests,
        }

    @staticmethod
    def format_status(status_id: int) -> str:
        """Convert a TestRail status ID to a human-readable string."""
        return STATUS_MAP.get(status_id, f"status_{status_id}")

    def get_recent_run_summary(
        self,
        project_id: Optional[int] = None,
        suite_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the summary of the most recent run. Returns None if not enabled or no runs."""
        if not self.enabled:
            return None
        runs = self.get_runs(project_id=project_id, suite_id=suite_id, limit=1)
        if not runs:
            logger.warning("No runs found for project_id=%s suite_id=%s", project_id, suite_id)
            return None
        return self.get_run_summary(runs[0]["id"])
