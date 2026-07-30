"""Resolve device->client, stamp received_ts, detect counter gaps.

Handles two socket message shapes from the BLE host:
  - cough event:  {"device_id", "cough_type", "event_ts": null, "counter"}
  - status line:  {"device_id", "status": "connected" | "disconnected"}

Events from unassigned devices are still stored (client_id = "unknown").
Live BLE events are stamped with the gateway's current UTC time. A provided
received_ts is accepted for local test/import scripts that replay history.

See design/gateway_app.md.
"""

from __future__ import annotations

import datetime
import logging

from .dao import Dao
from .fleet import Fleet


class EventProcessor:
    """Transforms raw BLE-host messages into stored cough_event rows."""

    def __init__(self, dao: Dao, fleet: Fleet) -> None:
        self.dao = dao
        self.fleet = fleet
        self.last_counter: dict[str, int] = {}

    def process(self, raw: dict) -> None:
        """Process one message from the BLE host socket."""
        device_id = raw.get("device_id")
        if not device_id:
            return

        if "status" in raw or raw.get("type") == "status":
            connected = raw.get("connected")
            status_str = raw.get("status")

            if connected is True or status_str == "connected":
                self.fleet.on_connect(device_id)
                new_status = "online"
            else:
                self.fleet.on_disconnect(device_id)
                new_status = "offline"

            client_id = raw.get("client_id")
            self.dao.upsert_device(device_id, status=new_status)
            if client_id:
                self.dao.set_client(device_id, client_id)
            return

        self.dao.upsert_device(device_id)
        device_info = self.dao.get_device(device_id)

        client_id = raw.get("client_id")
        if not client_id and device_info and device_info.get("client_id"):
            client_id = device_info["client_id"]

        if not client_id:
            client_id = "unknown"

        received_ts = raw.get("received_ts")
        if not received_ts:
            received_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

        event_ts = raw.get("event_ts")
        counter = raw.get("counter", 0)
        gap = self._detect_gap(device_id, counter)
        if gap > 0:
            logging.warning(
                f"[CHẨN ĐOÁN] Mạch {device_id} bị mất {gap} tiếng ho! "
                f"(Counter hiện tại: {counter})"
            )

        evt = {
            "device_id": device_id,
            "client_id": client_id,
            "cough_type": raw.get("cough_type"),
            "event_ts": event_ts,
            "received_ts": received_ts,
            "event_counter": counter,
        }
        self.dao.insert_event(evt)

    def _detect_gap(self, device_id: str, counter: int) -> int:
        """Return the number of missed events, handling uint16 wrap."""
        gap = 0
        if device_id in self.last_counter:
            last = self.last_counter[device_id]

            if counter > last:
                gap = counter - last - 1
            elif counter < last:
                gap = (65535 - last) + counter

        self.last_counter[device_id] = counter
        return gap
