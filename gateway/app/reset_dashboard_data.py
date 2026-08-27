"""Explicitly delete all patient, device, and telemetry rows from SQLite."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .database_path import resolve_database_path


RESET_TABLES = (
    "telemetry_receipts",
    "telemetry_outbox",
    "cough_events",
    "environment_readings",
    "client_settings",
    "devices",
)
AUTOINCREMENT_TABLES = (
    "cough_events",
    "environment_readings",
    "telemetry_outbox",
)


def reset_dashboard_data(
    db_path: str | Path | None = None,
    *,
    vacuum: bool = True,
) -> dict[str, int]:
    """Delete every dashboard row atomically and return prior row counts."""
    resolved = resolve_database_path(db_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Database does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Database path is not a file: {resolved}")

    connection = sqlite3.connect(str(resolved), timeout=5.0)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = set(RESET_TABLES) - present
        if missing:
            raise RuntimeError(
                "Database is missing required dashboard table(s): "
                + ", ".join(sorted(missing))
            )

        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in RESET_TABLES
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in RESET_TABLES:
                connection.execute(f"DELETE FROM {table}")
            if "sqlite_sequence" in present:
                placeholders = ", ".join("?" for _ in AUTOINCREMENT_TABLES)
                connection.execute(
                    f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                    AUTOINCREMENT_TABLES,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        if vacuum:
            connection.execute("VACUUM")
        return counts
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: GATEWAY_DB_PATH or repo-root cough_monitor.db)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion of every dashboard/patient row",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip reclaiming unused database pages after the transaction",
    )
    args = parser.parse_args(argv)
    resolved = resolve_database_path(args.db)
    if not args.yes:
        print(
            "ERROR: This command deletes ALL dashboard/patient data.\n"
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2

    print(f"Resetting dashboard database: {resolved}", flush=True)
    try:
        counts = reset_dashboard_data(resolved, vacuum=not args.no_vacuum)
    except (FileNotFoundError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "Reset complete: "
        + ", ".join(f"{table}={count}" for table, count in counts.items()),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
