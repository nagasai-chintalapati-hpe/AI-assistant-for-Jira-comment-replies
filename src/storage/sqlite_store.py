"""SQLite-backed persistence for drafts and processed webhook events."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SQLiteStore:
    """Simple persistent store for MVP state."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    draft_id TEXT PRIMARY KEY,
                    issue_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

    # Draft APIs
    def upsert_draft(self, draft: dict) -> None:
        draft_id = draft["draft_id"]
        issue_key = draft.get("issue_key", "")
        updated_at = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(draft)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO drafts(draft_id, issue_key, payload_json, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET
                    issue_key=excluded.issue_key,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (draft_id, issue_key, payload_json, updated_at),
            )

    def get_draft(self, draft_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    def list_drafts(self, issue_key: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if issue_key:
                rows = conn.execute(
                    "SELECT payload_json FROM drafts WHERE issue_key = ? ORDER BY updated_at DESC",
                    (issue_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM drafts ORDER BY updated_at DESC"
                ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def clear_drafts(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM drafts")

    # Event APIs (idempotency)
    def is_event_processed(self, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return row is not None

    def mark_event_processed(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_events(event_id, processed_at)
                VALUES(?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, datetime.now(timezone.utc).isoformat()),
            )

    def clear_processed_events(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM processed_events")
