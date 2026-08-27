"""Device assignment and online/offline/last-seen tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .dao import Dao
from .device_assignments import REAL_DEVICE_CLIENT_MAP, known_client_for_device


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class Fleet:
    """Manages stable BLE-address device IDs and patient assignment."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao
        self.ensure_known_assignments()

    def ensure_known_assignments(self) -> None:
        """Repair fixed real-device mappings on every gateway startup."""
        for device_id, client_id in REAL_DEVICE_CLIENT_MAP.items():
            current = self.dao.get_device(device_id)
            assignment_changed = (
                current is None or current.get("client_id") != client_id
            )
            assigned_at = (
                _now_iso()
                if assignment_changed
                else current.get("assigned_at")
            )
            self.dao.persist_device_assignment(
                device_id,
                client_id,
                assigned_at,
                backfill_existing=True,
            )

    def assign(self, device_id: str, client_id: str | None) -> str | None:
        """Persist assignment; fixed physical mappings cannot be overridden."""
        resolved_client = known_client_for_device(device_id) or client_id
        current = self.dao.get_device(str(device_id).strip().lower())
        assignment_changed = (
            current is None or current.get("client_id") != resolved_client
        )
        if resolved_client is None:
            assigned_at = None
        elif assignment_changed:
            assigned_at = _now_iso()
        else:
            assigned_at = current.get("assigned_at") if current else None
        self.dao.persist_device_assignment(
            device_id,
            resolved_client,
            assigned_at,
            backfill_existing=True,
        )
        return resolved_client

    def resolve_client(
        self,
        device_id: str,
        explicit_client: str | None = None,
    ) -> str:
        """Resolve fixed mapping first, then payload/current DB assignment."""
        fixed = known_client_for_device(device_id)
        if fixed:
            return fixed
        if explicit_client:
            return str(explicit_client)
        current = self.dao.get_device(str(device_id).strip().lower())
        if current and current.get("client_id"):
            return str(current["client_id"])
        return "unknown"

    @staticmethod
    def _with_known_client(device_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        fixed = known_client_for_device(device_id)
        return {**metadata, **({"client_id": fixed} if fixed else {})}

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
            **self._with_known_client(device_id, metadata),
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
            **self._with_known_client(device_id, metadata),
        )

    def touch(
        self,
        device_id: str,
        seen_at: str | None = None,
        **metadata: Any,
    ) -> None:
        """Any valid payload proves the node is online and recently seen."""
        self.on_connect(device_id, seen_at=seen_at, **metadata)
