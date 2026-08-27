"""Canonical assignments for known physical BreathSense devices."""

from __future__ import annotations


REAL_DEVICE_CLIENT_MAP = {
    "54:dc:e9:32:21:ac": "client_01",
    "64:02:8f:64:12:88": "client_08",
}


def known_client_for_device(device_id: str) -> str | None:
    """Return the fixed patient for a known BLE address."""
    return REAL_DEVICE_CLIENT_MAP.get(str(device_id).strip().lower())
