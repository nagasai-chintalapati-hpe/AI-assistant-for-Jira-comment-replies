"""Log lookup service — Jenkins API + local file scanning.

Provides log retrieval from:
  • Jenkins build console output (via REST API)
  • Local log directory (file-based grep)

Returns structured ``LogEntry`` objects for context enrichment.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

from src.config import settings
from src.models.rag import LogEntry

logger = logging.getLogger(__name__)


class LogLookupService:
    """Fetches log entries from Jenkins and local log files."""

    def __init__(
        self,
        jenkins_base_url: Optional[str] = None,
        jenkins_username: Optional[str] = None,
        jenkins_api_token: Optional[str] = None,
        log_dir: Optional[str] = None,
        default_time_window_hours: int = 24,
    ) -> None:
        self._jenkins_url = (
            jenkins_base_url or settings.log_lookup.jenkins_base_url
        ).rstrip("/")
        self._jenkins_user = jenkins_username or settings.log_lookup.jenkins_username
        self._jenkins_token = jenkins_api_token or settings.log_lookup.jenkins_api_token
        self._log_dir = log_dir or settings.log_lookup.log_dir
        self._time_window_hours = default_time_window_hours

    @property
    def jenkins_enabled(self) -> bool:
        return bool(self._jenkins_url and self._jenkins_user and self._jenkins_token)

    @property
    def local_enabled(self) -> bool:
        return bool(self._log_dir and Path(self._log_dir).is_dir())

    def fetch_jenkins_console(
        self,
        job_url: str,
        max_lines: int = 200,
    ) -> Optional[LogEntry]:
        """Fetch console output from a Jenkins build URL.

        Accepts URLs like:
            https://jenkins.example.com/job/my-job/42/console
            https://jenkins.example.com/job/my-job/42/

        Returns a LogEntry with the last *max_lines* of output,
        or None if the fetch fails.
        """
        if not self.jenkins_enabled:
            logger.info("Jenkins not configured — skipping log fetch")
            return None

        console_url = self._normalise_console_url(job_url)

        try:
            resp = requests.get(
                console_url,
                auth=(self._jenkins_user, self._jenkins_token),
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.text

            lines = text.splitlines()
            if len(lines) > max_lines:
                lines = lines[-max_lines:]

            return LogEntry(
                source="jenkins",
                timestamp=datetime.now(timezone.utc).isoformat(),
                message="\n".join(lines),
                correlation_id=self._extract_build_number(job_url),
            )
        except Exception as exc:
            logger.warning("Jenkins console fetch failed for %s: %s", job_url, exc)
            return None

    def fetch_jenkins_logs_for_urls(
        self,
        urls: list[str],
        max_lines: int = 200,
    ) -> list[LogEntry]:
        """Fetch console output for a list of Jenkins URLs."""
        entries: list[LogEntry] = []
        for url in urls:
            entry = self.fetch_jenkins_console(url, max_lines=max_lines)
            if entry:
                entries.append(entry)
        return entries

    def search_local_logs(
        self,
        pattern: str,
        time_window_hours: Optional[int] = None,
        max_entries: int = 20,
    ) -> list[LogEntry]:
        """Search local log files for lines matching *pattern*.

        Scans files modified within the time window (default from config).
        Returns up to *max_entries* matching log lines as LogEntry objects.
        """
        if not self.local_enabled:
            logger.info("Local log directory not configured — skipping")
            return []

        window = time_window_hours or self._time_window_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window)
        log_path = Path(self._log_dir)

        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            logger.warning("Invalid log search pattern '%s': %s", pattern, exc)
            return []

        entries: list[LogEntry] = []

        for fpath in sorted(log_path.rglob("*.log")):
            if not fpath.is_file():
                continue
            mtime = datetime.fromtimestamp(fpath.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue

            try:
                with open(fpath, "r", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if compiled.search(line):
                            entries.append(
                                LogEntry(
                                    source="file",
                                    timestamp=mtime.isoformat(),
                                    message=line.rstrip(),
                                    correlation_id=f"{fpath.name}:{line_num}",
                                )
                            )
                            if len(entries) >= max_entries:
                                return entries
            except Exception as exc:
                logger.warning("Error reading log file %s: %s", fpath, exc)

        return entries

    def get_build_metadata(
        self,
        job_url: str,
    ) -> Optional[dict[str, str]]:
        """Extract build metadata (commit, version, timestamp) from Jenkins.

        Calls the Jenkins build API JSON endpoint to extract:
          - commit SHA (from changesets or git parameters)
          - build display name / version
          - deployment timestamp
        """
        if not self.jenkins_enabled:
            return None

        api_url = self._normalise_api_url(job_url)

        try:
            resp = requests.get(
                api_url,
                auth=(self._jenkins_user, self._jenkins_token),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            commit = self._extract_commit(data)
            version = data.get("displayName", data.get("fullDisplayName", ""))
            deploy_ts = ""
            if data.get("timestamp"):
                deploy_ts = datetime.fromtimestamp(
                    data["timestamp"] / 1000, tz=timezone.utc
                ).isoformat()

            return {
                "commit": commit,
                "version": version,
                "deploy_ts": deploy_ts,
            }
        except Exception as exc:
            logger.warning("Jenkins build metadata fetch failed: %s", exc)
            return None

    # Helpers

    @staticmethod
    def _normalise_console_url(url: str) -> str:
        """Ensure the URL points to /consoleText (plain text output)."""
        url = url.rstrip("/")
        url = re.sub(r"/(console|consoleFull|consoleText)$", "", url)
        return f"{url}/consoleText"

    @staticmethod
    def _normalise_api_url(url: str) -> str:
        """Convert a Jenkins build URL to its JSON API endpoint."""
        url = url.rstrip("/")
        url = re.sub(r"/(console|consoleFull|consoleText)$", "", url)
        return f"{url}/api/json"

    @staticmethod
    def _extract_build_number(url: str) -> str:
        """Extract the build number from a Jenkins URL."""
        match = re.search(r"/(\d+)(?:/|$)", url)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_commit(build_data: dict) -> str:
        """Extract the first commit SHA from Jenkins build data."""
        # Try changeSets first
        for cs in build_data.get("changeSets", []):
            for item in cs.get("items", []):
                if item.get("commitId"):
                    return item["commitId"][:12]
        # Try build parameters
        for action in build_data.get("actions", []):
            for param in action.get("parameters", []):
                name = (param.get("name") or "").lower()
                if name in ("git_commit", "commit_sha", "sha"):
                    return str(param.get("value", ""))[:12]
        return ""
