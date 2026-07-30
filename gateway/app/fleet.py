"""Device assignment and online/offline/last-seen tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .dao import Dao


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class Fleet:
    """Manages stable BLE-address device IDs and patient assignment."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def assign(self, device_id: str, client_id: str | None) -> None:
        assigned_at = _now_iso() if client_id is not None else None
        self.dao.upsert_device(
            device_id, client_id=client_id, assigned_at=assigned_at
        )

    def on_connect(
        self,
        device_id: str,
        seen_at: str | None = None,
        **metadata: Any,
    ) -> None:
        self.dao.upsert_device(
            device_id,
            status="online",
            last_seen=seen_at or _now_iso(),
            **metadata,
        )

    def on_disconnect(
        self,
        device_id: str,
        seen_at: str | None = None,
        **metadata: Any,
    ) -> None:
        self.dao.upsert_device(
            device_id,
            status="offline",
            last_seen=seen_at or _now_iso(),
            **metadata,
        )

    def touch(
        self,
        device_id: str,
        seen_at: str | None = None,
        **metadata: Any,
    ) -> None:
        """Any valid payload proves the node is online and recently seen."""
        self.on_connect(device_id, seen_at=seen_at, **metadata)
