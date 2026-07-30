"""Data models used by the BLE host."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PendingNode:
    """Advertising node for which a connection is currently being opened."""

    address: str
    address_type: int
    name: str
    started_at: float


@dataclass
class ConnectionState:
    """Discovery and notification state for one connected xG26 node."""

    handle: int
    address: str
    address_type: int
    name: str

    target_service: Optional[int] = None

    cough_characteristic: Optional[int] = None
    cough_characteristic_uuid: Optional[str] = None
    cough_characteristic_properties: int = 0

    environment_characteristic: Optional[int] = None
    environment_characteristic_uuid: Optional[str] = None
    environment_characteristic_properties: int = 0

    phase: str = "discover_service"
    phase_deadline: float = 0.0
    status_reported: bool = False
