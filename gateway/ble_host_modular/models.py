"""Data models used by the BLE host."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ConnectionState:
    """State for the currently connected xG26 node."""

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
