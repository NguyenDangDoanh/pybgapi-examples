"""Thread-safe SQLite data access and in-place schema migration."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any


class Dao:
    """Data access for devices, cough events, and environment readings."""

    _DEVICE_FIELDS = {
        "name",
        "address_type",
        "client_id",
        "assigned_at",
        "status",
        "last_seen",
    }

    def __init__(self, db_path: str = "cough_monitor.db") -> None:
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def init_db(self, schema_path: str) -> None:
        """Open SQLite, apply DDL, then migrate databases made by older builds."""
        with self._lock:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            with open(schema_path, "r", encoding="utf-8") as schema_file:
                self.conn.executescript(schema_file.read())
            self._migrate_existing_database()
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not initialized. Call init_db() first.")
        return self.conn

    def _columns(self, table: str) -> set[str]:
        rows = self._get_conn().execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        if column not in self._columns(table):
            self._get_conn().execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def _migrate_existing_database(self) -> None:
        # CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
        for column, declaration in {
            "name": "TEXT",
            "address_type": "INTEGER",
        }.items():
            self._ensure_column("devices", column, declaration)

        for column, declaration in {
            "message_id": "TEXT",
            "session_id": "TEXT",
            "node_event_timestamp": "INTEGER",
            "timestamp_source": "TEXT",
            "flags": "INTEGER",
            "timestamp_valid": "INTEGER",
            "stage2_valid": "INTEGER",
            "prolonged": "INTEGER",
            "duration_s": "INTEGER",
            "payload_hex": "TEXT",
        }.items():
            self._ensure_column("cough_events", column, declaration)

        # Old rows may contain NULL event_ts. New ingestion always supplies it.
        self._get_conn().execute(
            "UPDATE cough_events SET event_ts = received_ts WHERE event_ts IS NULL"
        )
        self._get_conn().executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_cough_message_id
                ON cough_events(message_id) WHERE message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_cough_received
                ON cough_events(received_ts DESC);
            CREATE INDEX IF NOT EXISTS ix_cough_client_received
                ON cough_events(client_id, received_ts DESC);
            CREATE INDEX IF NOT EXISTS ix_cough_client_event
                ON cough_events(client_id, event_ts DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_environment_message_id
                ON environment_readings(message_id) WHERE message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_environment_device_received
                ON environment_readings(device_id, received_ts DESC);
            """
        )

    @staticmethod
    def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _clamp_limit(limit: int, maximum: int = 500) -> int:
        try:
            return min(max(int(limit), 1), maximum)
        except (TypeError, ValueError):
            return 50

    def insert_event(self, evt: dict[str, Any]) -> int | None:
        """Insert a cough event; return None when message_id was already stored."""
        sql = """
            INSERT OR IGNORE INTO cough_events (
                message_id, session_id, device_id, client_id, cough_type,
                event_ts, received_ts, event_counter, node_event_timestamp,
                timestamp_source, flags, timestamp_valid, stage2_valid,
                prolonged, duration_s, payload_hex
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            evt.get("message_id"),
            evt.get("session_id"),
            evt.get("device_id"),
            evt.get("client_id"),
            evt.get("cough_type"),
            evt.get("event_ts"),
            evt.get("received_ts"),
            evt.get("event_counter"),
            evt.get("node_event_timestamp"),
            evt.get("timestamp_source"),
            evt.get("flags"),
            evt.get("timestamp_valid"),
            evt.get("stage2_valid"),
            evt.get("prolonged"),
            evt.get("duration_s"),
            evt.get("payload_hex"),
        )
        with self._lock:
            cursor = self._get_conn().execute(sql, values)
            self._get_conn().commit()
            return int(cursor.lastrowid) if cursor.rowcount else None

    def insert_environment(self, reading: dict[str, Any]) -> int | None:
        sql = """
            INSERT OR IGNORE INTO environment_readings (
                message_id, session_id, device_id, client_id, event_ts,
                received_ts, temperature_c, humidity_percent,
                temperature_x100, humidity_x100, payload_hex
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            reading.get("message_id"),
            reading.get("session_id"),
            reading.get("device_id"),
            reading.get("client_id"),
            reading.get("event_ts"),
            reading.get("received_ts"),
            reading.get("temperature_c"),
            reading.get("humidity_percent"),
            reading.get("temperature_x100"),
            reading.get("humidity_x100"),
            reading.get("payload_hex"),
        )
        with self._lock:
            cursor = self._get_conn().execute(sql, values)
            self._get_conn().commit()
            return int(cursor.lastrowid) if cursor.rowcount else None

    def get_events(
        self,
        client_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return transport-ordered events for audit use.

        Time filters intentionally use ``received_ts``. Occurrence-time
        analytics must use :meth:`get_events_by_occurrence` instead.
        """
        sql = "SELECT * FROM cough_events WHERE client_id = ?"
        params: list[object] = [client_id]
        if start_time is not None:
            sql += " AND received_ts >= ?"
            params.append(start_time)
        if end_time is not None:
            sql += " AND received_ts <= ?"
            params.append(end_time)
        sql += " ORDER BY received_ts DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(self._clamp_limit(limit))
        with self._lock:
            return self._rows(self._get_conn().execute(sql, params))

    def get_events_by_occurrence(
        self,
        client_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        """Return cough bouts filtered by their captured occurrence time.

        ``event_ts`` is the bout start time. This keeps offline FIFO replay in
        the hour/day where the bout occurred instead of the reconnect window.
        """
        sql = "SELECT * FROM cough_events WHERE client_id = ?"
        params: list[object] = [client_id]
        if start_time is not None:
            sql += " AND event_ts >= ?"
            params.append(start_time)
        if end_time is not None:
            sql += " AND event_ts <= ?"
            params.append(end_time)
        direction = "DESC" if descending else "ASC"
        sql += f" ORDER BY event_ts {direction}, id {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(self._clamp_limit(limit))
        with self._lock:
            return self._rows(self._get_conn().execute(sql, params))

    def get_recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = self._clamp_limit(limit)
        with self._lock:
            return self._rows(
                self._get_conn().execute(
                    "SELECT * FROM cough_events ORDER BY received_ts DESC, id DESC LIMIT ?",
                    (limit,),
                )
            )

    def get_recent_environment(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = self._clamp_limit(limit)
        with self._lock:
            return self._rows(
                self._get_conn().execute(
                    "SELECT * FROM environment_readings ORDER BY received_ts DESC, id DESC LIMIT ?",
                    (limit,),
                )
            )

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_device(self, device_id: str, **kwargs: Any) -> None:
        """Insert a device and update only explicitly whitelisted columns."""
        updates = {key: value for key, value in kwargs.items() if key in self._DEVICE_FIELDS}
        unknown = set(kwargs) - self._DEVICE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported device field(s): {', '.join(sorted(unknown))}")

        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO devices (device_id, status) VALUES (?, 'offline') "
                "ON CONFLICT(device_id) DO NOTHING",
                (device_id,),
            )
            if updates:
                clause = ", ".join(f"{key} = ?" for key in updates)
                conn.execute(
                    f"UPDATE devices SET {clause} WHERE device_id = ?",
                    (*updates.values(), device_id),
                )
            conn.commit()

    def set_client(self, device_id: str, client_id: str | None) -> None:
        with self._lock:
            self._get_conn().execute(
                "UPDATE devices SET client_id = ? WHERE device_id = ?",
                (client_id, device_id),
            )
            self._get_conn().commit()

    def set_status(self, device_id: str, status: str, last_seen: str) -> None:
        with self._lock:
            self._get_conn().execute(
                "UPDATE devices SET status = ?, last_seen = ? WHERE device_id = ?",
                (status, last_seen, device_id),
            )
            self._get_conn().commit()

    def mark_all_offline(self) -> None:
        """Clear stale online flags after a gateway process restart/crash."""
        with self._lock:
            self._get_conn().execute("UPDATE devices SET status = 'offline'")
            self._get_conn().commit()

    def get_devices(self) -> list[dict[str, Any]]:
        sql = """
            SELECT d.*,
                (SELECT e.temperature_c FROM environment_readings e
                 WHERE e.device_id = d.device_id
                 ORDER BY e.received_ts DESC, e.id DESC LIMIT 1) AS temperature_c,
                (SELECT e.humidity_percent FROM environment_readings e
                 WHERE e.device_id = d.device_id
                 ORDER BY e.received_ts DESC, e.id DESC LIMIT 1) AS humidity_percent,
                (SELECT e.event_ts FROM environment_readings e
                 WHERE e.device_id = d.device_id
                 ORDER BY e.received_ts DESC, e.id DESC LIMIT 1) AS environment_ts
            FROM devices d
            ORDER BY d.device_id
        """
        with self._lock:
            return self._rows(self._get_conn().execute(sql))

    def list_clients(self) -> list[dict[str, Any]]:
        sql = """
            SELECT client_id, COUNT(*) AS total_events, MAX(received_ts) AS last_event
            FROM cough_events
            WHERE client_id IS NOT NULL AND client_id != 'unknown'
            GROUP BY client_id
            ORDER BY client_id
        """
        with self._lock:
            return self._rows(self._get_conn().execute(sql))

    def get_client_summaries(self) -> list[dict[str, Any]]:
        return self.list_clients()

    def get_client_settings(self, client_id: str) -> dict[str, Any]:
        """Return persisted per-patient dashboard settings."""
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM client_settings WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return {
                "client_id": client_id,
                "treatment_start_date": None,
                "updated_at": None,
            }
        return dict(row)

    def set_treatment_start_date(
        self, client_id: str, treatment_start_date: str | None
    ) -> dict[str, Any]:
        """Persist or clear the single treatment marker for one patient."""
        updated_at = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with self._lock:
            self._get_conn().execute(
                """
                INSERT INTO client_settings (
                    client_id, treatment_start_date, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    treatment_start_date = excluded.treatment_start_date,
                    updated_at = excluded.updated_at
                """,
                (client_id, treatment_start_date, updated_at),
            )
            self._get_conn().commit()
        return self.get_client_settings(client_id)

    def get_hourly_counts(self, client_id: str) -> list[dict[str, Any]]:
        start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        sql = """
            SELECT strftime('%Y-%m-%d %H:00:00', event_ts) AS time_bucket,
                   cough_type, COUNT(*) AS count
            FROM cough_events
            WHERE client_id = ? AND event_ts >= ?
            GROUP BY time_bucket, cough_type
            ORDER BY time_bucket ASC
        """
        with self._lock:
            return self._rows(self._get_conn().execute(sql, (client_id, start_time)))

    def get_daily_counts(self, client_id: str) -> list[dict[str, Any]]:
        start_time = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        sql = """
            SELECT strftime('%Y-%m-%d', event_ts) AS time_bucket,
                   cough_type, COUNT(*) AS count
            FROM cough_events
            WHERE client_id = ? AND event_ts >= ?
            GROUP BY time_bucket, cough_type
            ORDER BY time_bucket ASC
        """
        with self._lock:
            return self._rows(self._get_conn().execute(sql, (client_id, start_time)))
