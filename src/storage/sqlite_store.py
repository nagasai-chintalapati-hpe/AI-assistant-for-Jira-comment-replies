"""SQLite-backed draft store.

Provides persistent CRUD operations for drafts with full audit
traceability.  Thread-safe via ``check_same_thread=False``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.models.draft import Draft, DraftStatus

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS drafts (
    draft_id        TEXT PRIMARY KEY,
    issue_key       TEXT NOT NULL,
    comment_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    body            TEXT NOT NULL,
    classification  TEXT,
    confidence      REAL NOT NULL DEFAULT 0.0,
    status          TEXT NOT NULL DEFAULT 'generated',
    approved_by     TEXT,
    approved_at     TEXT,
    posted_at       TEXT,
    feedback        TEXT,
    data_json       TEXT NOT NULL   -- full Draft model as JSON
);

CREATE INDEX IF NOT EXISTS idx_drafts_issue_key ON drafts(issue_key);
CREATE INDEX IF NOT EXISTS idx_drafts_status    ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_created   ON drafts(created_at);
"""


class SQLiteDraftStore:
    """Persistent draft store backed by SQLite.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file.  Parent directories are created
        automatically.  Use ``":memory:"`` for an in-memory database
        (useful in tests).
    """

    def __init__(self, db_path: str = ".data/assistant.db") -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        logger.info("SQLite draft store ready (%s)", db_path)

    # Schema bootstrap

    def _create_tables(self) -> None:
        """Create tables and indexes if they don't exist."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # CRUD operations

    def save(self, draft: Draft, classification: Optional[str] = None) -> None:
        """Insert or replace a draft."""
        self._conn.execute(
            """INSERT OR REPLACE INTO drafts
               (draft_id, issue_key, comment_id, created_at, created_by,
                body, classification, confidence, status,
                approved_by, approved_at, posted_at, feedback, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft.draft_id,
                draft.issue_key,
                draft.in_reply_to_comment_id,
                draft.created_at.isoformat(),
                draft.created_by,
                draft.body,
                classification,
                draft.confidence_score,
                draft.status.value,
                draft.approved_by,
                draft.approved_at.isoformat() if draft.approved_at else None,
                draft.posted_at.isoformat() if draft.posted_at else None,
                None,  # feedback
                draft.model_dump_json(),
            ),
        )
        self._conn.commit()
        logger.debug("Saved draft %s for %s", draft.draft_id, draft.issue_key)

    def get(self, draft_id: str) -> Optional[dict]:
        """Return a single draft as a dict, or None."""
        row = self._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["data_json"])

    def list_all(
        self,
        issue_key: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List drafts with optional filters."""
        query = "SELECT data_json FROM drafts WHERE 1=1"
        params: list = []

        if issue_key:
            query += " AND issue_key = ?"
            params.append(issue_key)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    def count(
        self,
        issue_key: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        """Count drafts with optional filters."""
        query = "SELECT COUNT(*) as cnt FROM drafts WHERE 1=1"
        params: list = []

        if issue_key:
            query += " AND issue_key = ?"
            params.append(issue_key)
        if status:
            query += " AND status = ?"
            params.append(status)

        row = self._conn.execute(query, params).fetchone()
        return row["cnt"] if row else 0

    def update_status(
        self,
        draft_id: str,
        status: DraftStatus,
        approved_by: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> bool:
        """Update draft status (approve / reject).  Returns True if found."""
        now = datetime.now(timezone.utc).isoformat()

        # First update indexed columns
        result = self._conn.execute(
            """UPDATE drafts
               SET status = ?, approved_by = ?, approved_at = ?, feedback = ?
               WHERE draft_id = ?""",
            (status.value, approved_by, now if approved_by else None, feedback, draft_id),
        )

        if result.rowcount == 0:
            return False

        # Also update the JSON blob
        row = self._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row:
            data = json.loads(row["data_json"])
            data["status"] = status.value
            data["approved_by"] = approved_by
            data["approved_at"] = now if approved_by else None
            if feedback:
                data["feedback"] = feedback
            self._conn.execute(
                "UPDATE drafts SET data_json = ? WHERE draft_id = ?",
                (json.dumps(data), draft_id),
            )

        self._conn.commit()
        logger.info("Updated draft %s → %s", draft_id, status.value)
        return True

    def mark_posted(self, draft_id: str) -> bool:
        """Mark a draft as posted to Jira.  Returns True if found."""
        now = datetime.now(timezone.utc).isoformat()
        result = self._conn.execute(
            "UPDATE drafts SET posted_at = ? WHERE draft_id = ?",
            (now, draft_id),
        )
        if result.rowcount == 0:
            return False

        # Update JSON blob too
        row = self._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row:
            data = json.loads(row["data_json"])
            data["posted_at"] = now
            self._conn.execute(
                "UPDATE drafts SET data_json = ? WHERE draft_id = ?",
                (json.dumps(data), draft_id),
            )

        self._conn.commit()
        return True

    def update_body(self, draft_id: str, body: str) -> bool:
        """Update the draft body (human-edited text).  Returns True if found."""
        result = self._conn.execute(
            "UPDATE drafts SET body = ? WHERE draft_id = ?",
            (body, draft_id),
        )
        if result.rowcount == 0:
            return False

        row = self._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row:
            data = json.loads(row["data_json"])
            data["body"] = body
            self._conn.execute(
                "UPDATE drafts SET data_json = ? WHERE draft_id = ?",
                (json.dumps(data), draft_id),
            )

        self._conn.commit()
        logger.debug("Updated body for draft %s", draft_id)
        return True

    def delete(self, draft_id: str) -> bool:
        """Delete a draft.  Returns True if it existed."""
        result = self._conn.execute(
            "DELETE FROM drafts WHERE draft_id = ?", (draft_id,)
        )
        self._conn.commit()
        return result.rowcount > 0

    def clear(self) -> int:
        """Delete all drafts.  Returns the number removed."""
        result = self._conn.execute("DELETE FROM drafts")
        self._conn.commit()
        return result.rowcount

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    @property
    def db_path(self) -> str:
        return self._db_path
