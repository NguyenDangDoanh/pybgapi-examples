"""Normalize BLE JSON, track each node independently, and persist events."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .dao import Dao
from .fleet import Fleet

LOG = logging.getLogger(__name__)
_COUGH_TYPES = {0: "unknown", 1: "dry", 2: "wet"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_iso(value: Any, fallback: str | None = None) -> str:
    candidate = value or fallback
    if candidate:
        try:
            parsed = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
        except (TypeError, ValueError):
            LOG.warning("Invalid timestamp %r; using gateway time", candidate)
    return _now_iso()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class EventProcessor:
    """Accept current nested schema plus the older flat test-message shape."""

    def __init__(self, dao: Dao, fleet: Fleet) -> None:
        self.dao = dao
        self.fleet = fleet
        self.last_counter: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()

    def process(self, raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            LOG.warning("Skipped non-object socket payload")
            return
        normalized = self._normalize(raw)
        if normalized is None:
            return

        with self._lock:
            event_name = normalized["event"]
            if event_name == "status":
                self._process_status(normalized)
            elif event_name == "cough_event":
                self._process_cough(normalized)
            elif event_name == "environment_data":
                self._process_environment(normalized)
            else:
                LOG.warning("Unsupported event type %r", event_name)

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        device = raw.get("device") if isinstance(raw.get("device"), dict) else {}
        parsed = raw.get("parsed") if isinstance(raw.get("parsed"), dict) else {}
        device_id = (
            raw.get("device_id")
            or device.get("device_id")
            or device.get("address")
        )
        if not device_id:
            LOG.warning("Skipped message without device_id/address: %s", raw)
            return None

        event_name = raw.get("event") or raw.get("type")
        if event_name in (None, "cough") and (
            "cough_type" in raw or "event_counter" in parsed or "counter" in raw
        ):
            event_name = "cough_event"
        if event_name == "environment":
            event_name = "environment_data"
        if "status" in raw and event_name not in ("cough_event", "environment_data"):
            event_name = "status"

        received_ts = _canonical_iso(
            raw.get("received_at") or raw.get("received_ts")
        )
        event_ts = _canonical_iso(
            raw.get("event_ts") or parsed.get("event_timestamp_iso"),
            fallback=received_ts,
        )
        session_id = str(raw.get("session_id") or "legacy")
        message_id = raw.get("message_id") or self._fallback_message_id(raw)

        return {
            "event": event_name,
            "device_id": str(device_id).lower(),
            "device_name": device.get("name") or raw.get("device_name"),
            "address_type": _safe_int(
                device["address_type"]
                if "address_type" in device
                else raw.get("address_type")
            ),
            "client_id": raw.get("client_id"),
            "received_ts": received_ts,
            "event_ts": event_ts,
            "session_id": session_id,
            "message_id": str(message_id),
            "status": raw.get("status"),
            "connected": raw.get("connected"),
            "cough_type": raw.get("cough_type", parsed.get("cough_type_name", parsed.get("cough_type"))),
            "counter": _safe_int(raw.get("counter", parsed.get("event_counter"))),
            "node_event_timestamp": _safe_int(parsed.get("event_timestamp")),
            "timestamp_source": parsed.get("timestamp_source") or raw.get("timestamp_source") or "gateway_received",
            "temperature_c": raw.get("temperature_c", parsed.get("temperature_c")),
            "humidity_percent": raw.get("humidity_percent", parsed.get("humidity_percent")),
            "temperature_x100": _safe_int(raw.get("temperature_x100", parsed.get("temperature_x100"))),
            "humidity_x100": _safe_int(raw.get("humidity_x100", parsed.get("humidity_x100"))),
            "payload_hex": raw.get("payload_hex"),
        }

    @staticmethod
    def _fallback_message_id(raw: dict[str, Any]) -> str:
        canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "legacy-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _metadata(item: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if item.get("device_name"):
            metadata["name"] = item["device_name"]
        if item.get("address_type") is not None:
            metadata["address_type"] = item["address_type"]
        return metadata

    def _resolve_client(self, item: dict[str, Any]) -> str:
        explicit = item.get("client_id")
        if explicit:
            return str(explicit)
        current = self.dao.get_device(item["device_id"])
        if current and current.get("client_id"):
            return str(current["client_id"])
        return "unknown"

    def _process_status(self, item: dict[str, Any]) -> None:
        connected = item.get("connected") is True or item.get("status") in (
            "connected",
            "online",
        )
        if connected:
            self.fleet.on_connect(
                item["device_id"], item["received_ts"], **self._metadata(item)
            )
        else:
            self.fleet.on_disconnect(
                item["device_id"], item["received_ts"], **self._metadata(item)
            )
        if item.get("client_id"):
            self.dao.set_client(item["device_id"], str(item["client_id"]))

    def _process_cough(self, item: dict[str, Any]) -> None:
        self.fleet.touch(
            item["device_id"], item["received_ts"], **self._metadata(item)
        )
        client_id = self._resolve_client(item)
        counter = item.get("counter")
        if counter is not None:
            duplicate_counter, gap, reset = self._counter_result(
                item["device_id"], item["session_id"], counter
            )
            if duplicate_counter:
                LOG.info(
                    "Duplicate counter skipped: device=%s session=%s counter=%d",
                    item["device_id"],
                    item["session_id"],
                    counter,
                )
                return
            if gap:
                LOG.warning(
                    "Possible missed cough events: device=%s gap=%d current_counter=%d",
                    item["device_id"],
                    gap,
                    counter,
                )
            if reset:
                LOG.info(
                    "Counter reset detected: device=%s previous session/state -> %d",
                    item["device_id"],
                    counter,
                )

        cough_type = item.get("cough_type")
        if isinstance(cough_type, int):
            cough_type = _COUGH_TYPES.get(cough_type, "unknown")
        if cough_type not in ("dry", "wet", "unknown"):
            cough_type = "unknown"

        row_id = self.dao.insert_event(
            {
                "message_id": item["message_id"],
                "session_id": item["session_id"],
                "device_id": item["device_id"],
                "client_id": client_id,
                "cough_type": cough_type,
                "event_ts": item["event_ts"],
                "received_ts": item["received_ts"],
                "event_counter": counter,
                "node_event_timestamp": item.get("node_event_timestamp"),
                "timestamp_source": item.get("timestamp_source"),
                "payload_hex": item.get("payload_hex"),
            }
        )
        if row_id is None:
            LOG.info("Duplicate message_id ignored: %s", item["message_id"])

    def _process_environment(self, item: dict[str, Any]) -> None:
        if item.get("temperature_c") is None or item.get("humidity_percent") is None:
            LOG.warning("Skipped incomplete environment payload from %s", item["device_id"])
            return
        try:
            temperature_c = float(item["temperature_c"])
            humidity_percent = float(item["humidity_percent"])
        except (TypeError, ValueError):
            LOG.warning("Skipped non-numeric environment payload from %s", item["device_id"])
            return
        if not -50.0 <= temperature_c <= 100.0 or not 0.0 <= humidity_percent <= 100.0:
            LOG.warning(
                "Skipped out-of-range environment payload from %s: temperature=%s humidity=%s",
                item["device_id"],
                temperature_c,
                humidity_percent,
            )
            return
        self.fleet.touch(
            item["device_id"], item["received_ts"], **self._metadata(item)
        )
        client_id = self._resolve_client(item)
        self.dao.insert_environment(
            {
                "message_id": item["message_id"],
                "session_id": item["session_id"],
                "device_id": item["device_id"],
                "client_id": client_id,
                "event_ts": item["event_ts"],
                "received_ts": item["received_ts"],
                "temperature_c": temperature_c,
                "humidity_percent": humidity_percent,
                "temperature_x100": item.get("temperature_x100"),
                "humidity_x100": item.get("humidity_x100"),
                "payload_hex": item.get("payload_hex"),
            }
        )

    def _counter_result(
        self, device_id: str, session_id: str, counter: int
    ) -> tuple[bool, int, bool]:
        """Return duplicate, missed-count, reset; uint16 wrap is handled safely."""
        key = (device_id, session_id)
        previous = self.last_counter.get(key)
        self.last_counter[key] = counter
        if previous is None:
            return False, 0, False
        if counter == previous:
            return True, 0, False
        if counter > previous:
            return False, max(counter - previous - 1, 0), False
        # Only consider an actual uint16 wrap near the boundaries.
        if previous >= 65000 and counter <= 535:
            return False, (65536 - previous) + counter - 1, False
        return False, 0, True
