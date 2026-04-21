"""SQLite-backed persistent draft store with audit traceability."""

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
    original_body   TEXT,           -- AI-generated body before human edits
    classification  TEXT,
    confidence      REAL NOT NULL DEFAULT 0.0,
    status          TEXT NOT NULL DEFAULT 'generated',
    approved_by     TEXT,
    approved_at     TEXT,
    posted_at       TEXT,
    feedback        TEXT,
    rating          INTEGER,
    data_json       TEXT NOT NULL   -- full Draft model as JSON
);

CREATE INDEX IF NOT EXISTS idx_drafts_issue_key ON drafts(issue_key);
CREATE INDEX IF NOT EXISTS idx_drafts_status    ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_created   ON drafts(created_at);
"""


class SQLiteDraftStore:
    """Persistent draft store backed by SQLite."""

    def __init__(self, db_path: str = ".data/assistant.db") -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        logger.info("SQLite draft store ready (%s)", db_path)

    def _create_tables(self) -> None:
        """Create tables and indexes if they don't exist."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        """Add new columns to existing databases (idempotent migrations)."""
        migrations = [
            "ALTER TABLE drafts ADD COLUMN rating INTEGER",
            "ALTER TABLE drafts ADD COLUMN original_body TEXT",
        ]
        for sql in migrations:
            try:
                self._conn.execute(sql)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists — safe to ignore

    def save(self, draft: Draft, classification: Optional[str] = None) -> None:
        """Insert or replace a draft."""
        self._conn.execute(
            """INSERT OR REPLACE INTO drafts
               (draft_id, issue_key, comment_id, created_at, created_by,
                body, original_body, classification, confidence, status,
                approved_by, approved_at, posted_at, feedback, rating, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft.draft_id,
                draft.issue_key,
                draft.in_reply_to_comment_id,
                draft.created_at.isoformat(),
                draft.created_by,
                draft.body,
                draft.original_body or draft.body,  # preserve original AI text
                classification,
                draft.confidence_score,
                draft.status.value,
                draft.approved_by,
                draft.approved_at.isoformat() if draft.approved_at else None,
                draft.posted_at.isoformat() if draft.posted_at else None,
                None,  # feedback
                draft.rating,
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
            "UPDATE drafts SET status = ?, posted_at = ? WHERE draft_id = ?",
            (DraftStatus.POSTED.value, now, draft_id),
        )
        if result.rowcount == 0:
            return False

        # Update JSON blob too
        row = self._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row:
            data = json.loads(row["data_json"])
            data["status"] = DraftStatus.POSTED.value
            data["posted_at"] = now
            self._conn.execute(
                "UPDATE drafts SET data_json = ? WHERE draft_id = ?",
                (json.dumps(data), draft_id),
            )

        self._conn.commit()
        return True

    def update_body(self, draft_id: str, body: str) -> bool:
        """Update draft body text; preserves original_body.  Returns True if found."""
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
            # Preserve original_body — only update the final body
            if "original_body" not in data or data["original_body"] is None:
                data["original_body"] = data.get("body")  # late-set for older rows
            data["body"] = body
            self._conn.execute(
                "UPDATE drafts SET data_json = ? WHERE draft_id = ?",
                (json.dumps(data), draft_id),
            )

        self._conn.commit()
        logger.debug("Updated body for draft %s (original preserved)", draft_id)
        return True

    def find_recent_by_issue(
        self,
        issue_key: str,
        limit: int = 20,
        days: int = 180,
    ) -> list[dict]:
        """Return recent drafts for *issue_key* (newest first)."""
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        rows = self._conn.execute(
            """
            SELECT data_json FROM drafts
            WHERE issue_key = ? AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (issue_key, cutoff, limit),
        ).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    def purge_stale(self, days: int = 30) -> int:
        """Delete generated drafts older than *days* days; returns count deleted."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        result = self._conn.execute(
            "DELETE FROM drafts WHERE status = 'generated' AND created_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        deleted = result.rowcount
        if deleted:
            logger.info(
                "Purged %d stale GENERATED draft(s) older than %d days", deleted, days
            )
        return deleted

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

    def save_rating(self, draft_id: str, rating: int) -> bool:
        """Save a 1–5 star rating for a draft.  Returns True if found."""
        if not (1 <= rating <= 5):
            raise ValueError(f"Rating must be 1–5, got {rating}")

        row = self._conn.execute(
            "SELECT data_json FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if not row:
            return False

        data = json.loads(row["data_json"])
        data["rating"] = rating
        self._conn.execute(
            "UPDATE drafts SET rating = ?, data_json = ? WHERE draft_id = ?",
            (rating, json.dumps(data), draft_id),
        )
        self._conn.commit()
        logger.info("Saved rating %d for draft %s", rating, draft_id)
        return True

    def get_metrics(self) -> dict:
        """Return aggregated draft quality and processing metrics."""
        total = self.count()
        approved = self.count(status="approved")
        rejected = self.count(status="rejected")
        pending = self.count(status="generated")

        conf_row = self._conn.execute(
            "SELECT AVG(confidence) as avg_conf FROM drafts"
        ).fetchone()
        avg_confidence = round(conf_row["avg_conf"] or 0.0, 3)

        rating_row = self._conn.execute(
            "SELECT AVG(rating) as avg_rating FROM drafts WHERE rating IS NOT NULL"
        ).fetchone()
        avg_rating = (
            round(rating_row["avg_rating"], 2)
            if rating_row and rating_row["avg_rating"] is not None
            else None
        )

        acceptance_rate = round(approved / total * 100, 1) if total > 0 else 0.0

        type_rows = self._conn.execute(
            "SELECT classification, COUNT(*) as cnt FROM drafts "
            "GROUP BY classification ORDER BY cnt DESC"
        ).fetchall()
        by_classification = {
            (r["classification"] or "unknown"): r["cnt"] for r in type_rows
        }

        hall_row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM drafts "
            "WHERE json_extract(data_json, '$.hallucination_flag') = 1"
        ).fetchone()
        hallucination_flagged = hall_row["cnt"] if hall_row else 0

        # Drafts where a human edited the body before approving
        edited_row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM drafts "
            "WHERE original_body IS NOT NULL AND body != original_body"
        ).fetchone()
        edited_count = edited_row["cnt"] if edited_row else 0
        pct_edited = round(edited_count / approved * 100, 1) if approved > 0 else 0.0

        # Pipeline latency
        latency_row = self._conn.execute(
            "SELECT AVG(json_extract(data_json, '$.pipeline_duration_ms')) as avg_ms "
            "FROM drafts"
        ).fetchone()
        avg_pipeline_ms = (
            round(latency_row["avg_ms"], 1)
            if latency_row and latency_row["avg_ms"] is not None
            else None
        )

        # Redaction stats
        redaction_row = self._conn.execute(
            "SELECT SUM(json_extract(data_json, '$.redaction_count')) as total "
            "FROM drafts"
        ).fetchone()
        total_redactions = int(redaction_row["total"] or 0) if redaction_row else 0

        return {
            "total_drafts": total,
            "approved": approved,
            "rejected": rejected,
            "pending": pending,
            "acceptance_rate_pct": acceptance_rate,
            "avg_confidence": avg_confidence,
            "avg_rating": avg_rating,
            "hallucination_flagged": hallucination_flagged,
            "drafts_edited_before_approval": edited_count,
            "pct_approved_drafts_edited": pct_edited,
            "avg_pipeline_duration_ms": avg_pipeline_ms,
            "total_redactions": total_redactions,
            "by_classification": by_classification,
        }

    def get_daily_volume(self, days: int = 30) -> list[dict]:
        """Return daily draft counts for the last *days* days.

        Each entry: ``{"date": "2026-03-18", "total": 5, "approved": 3, "rejected": 1}``
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        rows = self._conn.execute(
            """
            SELECT
                DATE(created_at) as dt,
                COUNT(*)         as total,
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected
            FROM drafts
            WHERE DATE(created_at) >= ?
            GROUP BY DATE(created_at)
            ORDER BY dt
            """,
            (cutoff,),
        ).fetchall()
        return [
            {
                "date": r["dt"],
                "total": r["total"],
                "approved": r["approved"],
                "rejected": r["rejected"],
            }
            for r in rows
        ]

    def get_severity_challenges(self, limit: int = 50) -> list[dict]:
        """Return drafts that have a severity challenge attached.

        Each entry is the full draft JSON with a non-null
        ``severity_challenge`` field.
        """
        rows = self._conn.execute(
            """
            SELECT data_json FROM drafts
            WHERE json_extract(data_json, '$.severity_challenge') IS NOT NULL
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    def get_top_issues(self, limit: int = 10) -> list[dict]:
        """Return the most-active issue keys by draft count."""
        rows = self._conn.execute(
            """
            SELECT issue_key, COUNT(*) as cnt,
                   SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
                   AVG(confidence) as avg_conf
            FROM drafts
            GROUP BY issue_key
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "issue_key": r["issue_key"],
                "count": r["cnt"],
                "approved": r["approved"],
                "avg_confidence": round(r["avg_conf"] or 0, 3),
            }
            for r in rows
        ]

    def get_repos_stats(self) -> dict[str, int]:
        """Return PR-count per repo from drafts with ``repos_searched``."""
        rows = self._conn.execute(
            """
            SELECT data_json FROM drafts
            WHERE json_extract(data_json, '$.repos_searched') IS NOT NULL
            """
        ).fetchall()
        repo_counts: dict[str, int] = {}
        for r in rows:
            data = json.loads(r["data_json"])
            for repo in (data.get("repos_searched") or []):
                repo_counts[repo] = repo_counts.get(repo, 0) + 1
        return repo_counts

    def get_avg_response_time_by_day(self, days: int = 30) -> list[dict]:
        """Return average pipeline duration per day."""
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        rows = self._conn.execute(
            """
            SELECT
                DATE(created_at) as dt,
                AVG(json_extract(data_json, '$.pipeline_duration_ms')) as avg_ms
            FROM drafts
            WHERE DATE(created_at) >= ?
            GROUP BY DATE(created_at)
            ORDER BY dt
            """,
            (cutoff,),
        ).fetchall()
        return [
            {
                "date": r["dt"],
                "avg_ms": round(r["avg_ms"] or 0, 1),
            }
            for r in rows
        ]


_IDEMPOTENCY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS seen_events (
    event_id  TEXT PRIMARY KEY,
    seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_events_seen_at ON seen_events(seen_at);
"""


class SQLiteIdempotencyStore:
    """Persistent event-ID deduplication store backed by SQLite."""

    def __init__(self, db_path: str = ".data/assistant.db") -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_IDEMPOTENCY_SCHEMA)
        self._conn.commit()
        logger.info("Idempotency store ready (%s)", db_path)

    def is_seen(self, event_id: str) -> bool:
        """Return True if *event_id* has already been recorded."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, event_id: str) -> None:
        """Record *event_id* as processed.  Silently ignores duplicates."""
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_events (event_id, seen_at) VALUES (?, ?)",
            (event_id, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def clear(self) -> int:
        """Remove all records.  Returns count deleted (useful in tests)."""
        result = self._conn.execute("DELETE FROM seen_events")
        self._conn.commit()
        return result.rowcount

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
