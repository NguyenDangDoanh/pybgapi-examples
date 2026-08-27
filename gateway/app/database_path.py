"""Resolve the one SQLite path shared by gateway maintenance commands."""

from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = REPO_ROOT / "cough_monitor.db"


def resolve_database_path(value: str | os.PathLike[str] | None = None) -> Path:
    """Return an absolute database path independent of the current directory.

    An explicit value takes precedence, followed by ``GATEWAY_DB_PATH`` and
    then the repository-root default. Relative configured paths are anchored
    at the repository root so the gateway, simulator, and reset command cannot
    silently open different databases merely because they start in different
    working directories.
    """
    configured = value if value is not None else os.environ.get("GATEWAY_DB_PATH")
    path = Path(configured).expanduser() if configured else DEFAULT_DATABASE_PATH
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()
