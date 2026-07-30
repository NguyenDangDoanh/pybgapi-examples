"""Fleet manager — client ID assignment and online/offline tracking.

Online/offline is driven by BLE connection state reported by the BLE host
(no heartbeat): a device is online while the gateway holds a connection to it,
and offline once the connection closes.

See design/gateway_app.md.
"""

from __future__ import annotations

import datetime

from .dao import Dao


class Fleet:
    """Manages device-to-patient assignment and liveness status."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def assign(self, device_id: str, client_id: str | None) -> None:
        """Assign a patient client_id to a device, or unassign on discharge.

        client_id=None clears the mapping (patient discharged, device reusable).
        Stamps assigned_at with the current UTC time.
        """
        assigned_at = None
        if client_id is not None:
            assigned_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        self.dao.upsert_device(
            device_id,
            client_id=client_id,
            assigned_at=assigned_at
        )

    def on_connect(self, device_id: str) -> None:
        """Mark a device online and update last_seen.

        Called when the BLE host reports a connection opened.
        """
        last_seen = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.dao.upsert_device(
            device_id,
            status="online",
            last_seen=last_seen
        )

    def on_disconnect(self, device_id: str) -> None:
        """Mark a device offline.

        Called when the BLE host reports a connection closed.
        """
        self.dao.upsert_device(
            device_id,
            status="offline"
        )
