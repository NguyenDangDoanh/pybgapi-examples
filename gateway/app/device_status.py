"""Freshness fallback for persisted device connectivity state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .dao import Dao

DEVICE_STATUS_STALE_SECONDS = 30.0


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def expire_stale_device_status(
    dao: Dao,
    now: datetime | None = None,
) -> int:
    """Mark devices without recent gateway proof of life offline."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(seconds=DEVICE_STATUS_STALE_SECONDS)
    return dao.mark_stale_devices_offline(_utc_iso(cutoff))
