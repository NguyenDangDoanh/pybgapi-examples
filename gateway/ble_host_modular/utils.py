"""General helpers for UUIDs, Bluetooth addresses, and timestamps."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def utc_now_iso() -> str:
    """Return a UTC timestamp with millisecond precision."""
    return _to_utc_iso(datetime.now(timezone.utc))


def canonical_utc_iso(value: str | None, fallback: str | None = None) -> str:
    """Normalize an ISO timestamp to UTC, falling back to a known value/now."""
    candidate = value or fallback
    if candidate:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return _to_utc_iso(parsed)
        except (TypeError, ValueError):
            pass
    return utc_now_iso()


def resolve_event_timestamp(
    node_timestamp_seconds: int | None,
    received_at: str,
) -> tuple[str, str]:
    """Use a positive node Unix timestamp; zero falls back to receive time."""
    received_iso = canonical_utc_iso(received_at)
    try:
        node_timestamp = int(node_timestamp_seconds)
        if node_timestamp > 0:
            node_dt = datetime.fromtimestamp(
                node_timestamp, tz=timezone.utc
            )
            return _to_utc_iso(node_dt), "node_unix_seconds"
    except (OverflowError, OSError, TypeError, ValueError):
        pass

    return received_iso, "gateway_received"


def normalize_uuid(uuid_text: str) -> str:
    """Normalize a UUID for display and comparison."""
    value = uuid_text.strip().lower().replace("0x", "").replace("-", "")
    if len(value) not in (4, 32):
        raise argparse.ArgumentTypeError(
            "UUID must be a 16-bit UUID (4 hex digits) or "
            "128-bit UUID (32 hex digits)."
        )
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid UUID: {uuid_text}") from exc
    return value


def uuid_to_bgapi_bytes(uuid_text: str) -> bytes:
    """Convert a normalized UUID to BGAPI little-endian byte order."""
    return bytes.fromhex(normalize_uuid(uuid_text))[::-1]


def bgapi_uuid_to_text(uuid_value: bytes) -> str:
    """Convert BGAPI little-endian UUID bytes to readable text."""
    raw = bytes(uuid_value)[::-1].hex()
    if len(raw) == 4:
        return raw
    return (
        f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-"
        f"{raw[16:20]}-{raw[20:32]}"
    )


def normalize_address(address: Any) -> str:
    """Return a Bluetooth address as lowercase aa:bb:cc:dd:ee:ff."""
    if isinstance(address, (bytes, bytearray)):
        raw = bytes(address)
        if len(raw) != 6:
            raise ValueError(f"Expected 6 address bytes, received {len(raw)}")
        return ":".join(f"{byte:02x}" for byte in raw)

    text = str(address).strip().lower().replace("-", ":")
    compact = text.replace(":", "")
    if len(compact) != 12:
        raise ValueError(f"Invalid Bluetooth address: {address}")
    bytes.fromhex(compact)
    return ":".join(
        compact[index:index + 2] for index in range(0, 12, 2)
    )
