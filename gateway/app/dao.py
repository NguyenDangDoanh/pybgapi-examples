"""SQLite data access layer — the only module that touches SQL.

All other gateway modules take and return plain dicts.  init_db is idempotent
(CREATE TABLE IF NOT EXISTS).  See design/gateway_app.md.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone


class Dao:
    """Thin wrapper over SQLite for devices and cough_events tables."""

    def __init__(self, db_path: str = "cough_monitor.db") -> None:
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def init_db(self, schema_path: str) -> None:
        """Open the database and run schema.sql.

        Must be safe to call multiple times (idempotent DDL).
        The connection is shared by the socket and Flask threads, so every
        database operation is protected by the same re-entrant lock.
        """
        with self._lock:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self.conn.execute("PRAGMA journal_mode = WAL")
            with open(schema_path, "r", encoding="utf-8") as f:
                self.conn.executescript(f.read())
            self.conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not initialized. Call init_db() first.")
        return self.conn

    def insert_event(self, evt: dict) -> int:
        """Insert one cough event row."""
        sql = """
            INSERT INTO cough_events (
                device_id, client_id, cough_type, event_ts, received_ts, event_counter
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(sql, (
                evt.get("device_id"),
                evt.get("client_id"),
                evt.get("cough_type"),
                evt.get("event_ts"),
                evt.get("received_ts"),
                evt.get("event_counter")
            ))
            conn.commit()
            return cursor.lastrowid

    def get_events(
        self, client_id: str, start_time: str | None = None, end_time: str | None = None
    ) -> list[dict]:
        """Return cough events for a client, optionally filtered by time range."""
        sql = "SELECT * FROM cough_events WHERE client_id = ?"
        params: list[object] = [client_id]

        if start_time is not None:
            sql += " AND received_ts >= ?"
            params.append(start_time)

        if end_time is not None:
            sql += " AND received_ts <= ?"
            params.append(end_time)

        sql += " ORDER BY received_ts DESC"

        with self._lock:
            cursor = self._get_conn().execute(sql, params)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_recent_events(self, limit: int = 100) -> list[dict]:
        """Return the latest events across the fleet (newest first)."""
        sql = "SELECT * FROM cough_events ORDER BY received_ts DESC LIMIT ?"
        with self._lock:
            cursor = self._get_conn().execute(sql, (limit,))
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_device(self, device_id: str) -> dict | None:
        """Look up a device record, or None if it has never been seen."""
        sql = "SELECT * FROM devices WHERE device_id = ?"
        with self._lock:
            cursor = self._get_conn().execute(sql, (device_id,))
            row = cursor.fetchone()
            if row:
                columns = [col[0] for col in cursor.description]
                return dict(zip(columns, row))
            return None

    def upsert_device(self, device_id: str, **kwargs) -> None:
        """Insert a device row if needed and update any provided fields."""
        sql_insert = """
            INSERT INTO devices (device_id, status)
            VALUES (?, 'offline')
            ON CONFLICT(device_id) DO NOTHING
        """

        with self._lock:
            conn = self._get_conn()
            conn.execute(sql_insert, (device_id,))

            if kwargs:
                set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
                values = list(kwargs.values()) + [device_id]
                sql_update = f"UPDATE devices SET {set_clause} WHERE device_id = ?"
                conn.execute(sql_update, values)

            conn.commit()

    def set_client(self, device_id: str, client_id: str | None) -> None:
        """Assign or clear the patient/client mapping on a device."""
        sql = "UPDATE devices SET client_id = ? WHERE device_id = ?"
        with self._lock:
            conn = self._get_conn()
            conn.execute(sql, (client_id, device_id))
            conn.commit()

    def set_status(self, device_id: str, status: str, last_seen: str) -> None:
        """Update online/offline status and the last_seen timestamp."""
        sql = "UPDATE devices SET status = ?, last_seen = ? WHERE device_id = ?"
        with self._lock:
            conn = self._get_conn()
            conn.execute(sql, (status, last_seen, device_id))
            conn.commit()

    def get_devices(self) -> list[dict]:
        """Return all devices with status, client_id, and last_seen."""
        sql = "SELECT * FROM devices ORDER BY device_id"
        with self._lock:
            cursor = self._get_conn().execute(sql)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def list_clients(self) -> list[dict]:
        """Return distinct clients with summary stats."""
        sql = """
            SELECT
                client_id,
                COUNT(*) as total_events,
                MAX(received_ts) as last_event
            FROM cough_events
            WHERE client_id IS NOT NULL
            GROUP BY client_id
        """
        with self._lock:
            cursor = self._get_conn().execute(sql)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_client_summaries(self) -> list[dict]:
        return self.list_clients()

    def get_hourly_counts(self, client_id: str) -> list[dict]:
        """Count coughs by hour over the latest 24 hours for one client."""
        start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        sql = """
            SELECT
                strftime('%Y-%m-%d %H:00:00', received_ts) as time_bucket,
                cough_type,
                COUNT(*) as count
            FROM cough_events
            WHERE client_id = ? AND received_ts >= ?
            GROUP BY time_bucket, cough_type
            ORDER BY time_bucket ASC
        """
        with self._lock:
            cursor = self._get_conn().execute(sql, (client_id, start_time))
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_daily_counts(self, client_id: str) -> list[dict]:
        """Count coughs by day over the latest 7 days for one client."""
        start_time = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        sql = """
            SELECT
                strftime('%Y-%m-%d', received_ts) as time_bucket,
                cough_type,
                COUNT(*) as count
            FROM cough_events
            WHERE client_id = ? AND received_ts >= ?
            GROUP BY time_bucket, cough_type
            ORDER BY time_bucket ASC
        """
        with self._lock:
            cursor = self._get_conn().execute(sql, (client_id, start_time))
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
