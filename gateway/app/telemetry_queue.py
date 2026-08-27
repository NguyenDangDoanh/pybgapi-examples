"""Durable SQLite store-and-forward queue for gateway telemetry."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class TelemetryQueue:
    """Persistent FIFO using a short-lived SQLite connection per operation.

    The BLE ingest thread and upload worker never share a sqlite3.Connection.
    WAL plus busy_timeout keeps their short transactions independent.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry_outbox (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id        TEXT UNIQUE NOT NULL,
                    event_ts        TEXT NOT NULL,
                    payload_json    TEXT NOT NULL,
                    sent            INTEGER NOT NULL DEFAULT 0,
                    retry_count     INTEGER NOT NULL DEFAULT 0,
                    last_error      TEXT,
                    last_attempt_at TEXT,
                    sent_at         TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_telemetry_outbox_pending
                    ON telemetry_outbox(sent, id);
                CREATE TABLE IF NOT EXISTS telemetry_receipts (
                    event_id    TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def enqueue(
        self,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        event_ts: str | None = None,
    ) -> tuple[str, bool]:
        """Commit one event before it becomes eligible for remote upload."""
        durable_payload = dict(payload)
        resolved_id = str(event_id or durable_payload.get("message_id") or uuid.uuid4())
        resolved_ts = str(
            event_ts
            or durable_payload.get("event_ts")
            or durable_payload.get("received_at")
            or _now_iso()
        )
        durable_payload["message_id"] = resolved_id
        encoded = json.dumps(
            durable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO telemetry_outbox (
                    event_id, event_ts, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (resolved_id, resolved_ts, encoded, _now_iso()),
            )
            conn.commit()
            return resolved_id, bool(cursor.rowcount)

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        batch_size = min(max(int(limit), 1), 1000)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, event_id, event_ts, payload_json, retry_count,
                       created_at
                FROM telemetry_outbox
                WHERE sent = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (batch_size,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def mark_sent(self, row_id: int, event_id: str) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE telemetry_outbox
                SET sent = 1, sent_at = ?, last_error = NULL,
                    last_attempt_at = ?
                WHERE id = ? AND event_id = ? AND sent = 0
                """,
                (_now_iso(), _now_iso(), int(row_id), str(event_id)),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def mark_failed(self, row_id: int, error: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE telemetry_outbox
                SET retry_count = retry_count + 1,
                    last_error = ?, last_attempt_at = ?
                WHERE id = ? AND sent = 0
                """,
                (str(error)[:1000], _now_iso(), int(row_id)),
            )
            conn.commit()

    def pending_count(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM telemetry_outbox WHERE sent = 0"
            ).fetchone()
        return int(row[0]) if row else 0

    def stats(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN sent = 0 THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN sent = 1 THEN 1 ELSE 0 END) AS sent
                FROM telemetry_outbox
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "pending": int(row["pending"] or 0),
            "sent": int(row["sent"] or 0),
        }

    def receipt_exists(self, event_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM telemetry_receipts WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        return row is not None

    def record_receipt(self, event_id: str) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO telemetry_receipts (event_id, received_at)
                VALUES (?, ?)
                """,
                (str(event_id), _now_iso()),
            )
            conn.commit()
            return bool(cursor.rowcount)
