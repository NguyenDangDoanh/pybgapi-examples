#!/usr/bin/env python3
"""
BreathSense BLE Central for Raspberry Pi + BGM220 NCP.

Role
----
- Raspberry Pi runs this Python host.
- BGM220 runs Silicon Labs Bluetooth NCP firmware.
- EFR32xG26 advertises as a BLE peripheral and sends GATT notifications.
- This program scans, connects, discovers the target characteristic,
  subscribes to notifications, and forwards every notification to a backend.

Backend contract
----------------
Notifications are forwarded as UTF-8 JSON Lines (one JSON object + "\n")
through a Unix domain stream socket.

The backend is optional. Without --backend-socket, notifications are still
printed to stdout, which allows BLE integration to be tested independently.

Expected BreathSense event payload
----------------------------------
The current repository contract uses an 8-byte little-endian payload:

    uint8  flags
    uint8  cough_type
    uint32 event_timestamp
    uint16 event_counter

Equivalent Python struct format: <BBIH

Payloads with a different length are NOT discarded. They are forwarded with
payload_hex and payload_length, while parsed remains null.

Example
-------
python ble_central.py \
  /dev/serial/by-id/usb-Silicon_Labs_J-Link_OB_000440210672-if00 \
  --name-prefix MyDevice \
  --service-uuid b5e00001-7a4b-4c6d-9e10-112233445566 \
  --notify-uuid b5e00002-7a4b-4c6d-9e10-112233445566 \
  --backend-socket /run/breathsense/backend.sock \
  -l DEBUG
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import bgapi  # Provided by the pybgapi package.


LOG = logging.getLogger("breathsense.ble_central")

# Silicon Labs Bluetooth API enum values.
PHY_1M = 1
SCAN_ACTIVE = 1
SCANNER_DISCOVER_OBSERVATION = 2
GATT_NOTIFICATION = 1
GATT_PROPERTY_NOTIFY = 0x10

# ATT opcodes from sl_bt_evt_gatt_characteristic_value.
ATT_READ_RESPONSE = 0x0B
ATT_HANDLE_VALUE_NOTIFICATION = 0x1B
ATT_HANDLE_VALUE_INDICATION = 0x1D

# Advertising Data types.
AD_TYPE_SHORT_NAME = 0x08
AD_TYPE_COMPLETE_NAME = 0x09

BOOT_TIMEOUT_SECONDS = 8.0
RECONNECT_DELAY_SECONDS = 1.0
BACKEND_RETRY_SECONDS = 2.0
BACKEND_QUEUE_LIMIT = 500

# flags, cough_type, event_timestamp, event_counter
EVENT_STRUCT = struct.Struct("<BBIH")


def utc_now_iso() -> str:
    """Return an RFC 3339-like UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_uuid(uuid_text: str) -> str:
    """Normalize a UUID for display and comparison."""
    value = uuid_text.strip().lower().replace("0x", "").replace("-", "")
    if len(value) not in (4, 32):
        raise argparse.ArgumentTypeError(
            "UUID must be a 16-bit UUID (4 hex digits) or 128-bit UUID (32 hex digits)."
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
    """Convert BGAPI little-endian UUID bytes to a readable UUID string."""
    raw = bytes(uuid_value)[::-1].hex()
    if len(raw) == 4:
        return raw
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


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
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def extract_advertised_name(data: bytes) -> Optional[str]:
    """Extract the shortened or complete local name from advertising data."""
    offset = 0
    raw = bytes(data)

    while offset < len(raw):
        field_length = raw[offset]
        if field_length == 0:
            break

        field_end = offset + 1 + field_length
        if field_end > len(raw) or field_length < 1:
            break

        ad_type = raw[offset + 1]
        ad_value = raw[offset + 2:field_end]
        if ad_type in (AD_TYPE_SHORT_NAME, AD_TYPE_COMPLETE_NAME):
            return ad_value.decode("utf-8", errors="replace")

        offset = field_end

    return None


def parse_event_payload(payload: bytes) -> Optional[dict[str, int | str]]:
    """Parse the current 8-byte BreathSense cough-event contract."""
    if len(payload) != EVENT_STRUCT.size:
        return None

    flags, cough_type, event_timestamp, event_counter = EVENT_STRUCT.unpack(payload)
    cough_type_name = {
        0: "unknown",
        1: "dry",
        2: "wet",
    }.get(cough_type, "reserved")

    return {
        "flags": flags,
        "cough_type": cough_type,
        "cough_type_name": cough_type_name,
        "event_timestamp": event_timestamp,
        "event_counter": event_counter,
    }


class JsonLineBackend:
    """Reconnectable Unix-domain JSON Lines client with an in-memory retry queue."""

    def __init__(
        self,
        socket_path: Optional[str],
        queue_limit: int = BACKEND_QUEUE_LIMIT,
    ) -> None:
        self.socket_path = socket_path
        self._socket: Optional[socket.socket] = None
        self._next_retry_at = 0.0
        self._queue: deque[bytes] = deque(maxlen=queue_limit)

    @property
    def enabled(self) -> bool:
        return bool(self.socket_path)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _connect(self) -> bool:
        if not self.socket_path:
            return False

        now = time.monotonic()
        if now < self._next_retry_at:
            return False

        self.close()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)

        try:
            client.connect(self.socket_path)
        except OSError as exc:
            client.close()
            self._next_retry_at = now + BACKEND_RETRY_SECONDS
            LOG.warning("Backend socket unavailable (%s): %s", self.socket_path, exc)
            return False

        client.settimeout(None)
        self._socket = client
        self._next_retry_at = 0.0
        LOG.info("Connected to backend socket: %s", self.socket_path)
        return True

    @staticmethod
    def _encode(message: dict[str, Any]) -> bytes:
        return (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _send_encoded(self, encoded: bytes) -> bool:
        if self._socket is None and not self._connect():
            return False

        try:
            assert self._socket is not None
            self._socket.sendall(encoded)
            return True
        except OSError as exc:
            LOG.warning("Backend send failed: %s", exc)
            self.close()
            return False

    def send(self, message: dict[str, Any]) -> bool:
        """
        Deliver a JSON object or queue it temporarily when the backend is offline.

        The queue is intentionally bounded. When full, the oldest queued message
        is discarded so BLE reception is never blocked indefinitely.
        """
        if not self.enabled:
            return False

        self.flush_pending()

        encoded = self._encode(message)
        if self._send_encoded(encoded):
            return True

        queue_was_full = len(self._queue) == self._queue.maxlen
        self._queue.append(encoded)
        if queue_was_full:
            LOG.error(
                "Backend queue full; oldest message dropped. queued=%d",
                len(self._queue),
            )
        else:
            LOG.warning(
                "Backend offline; message queued. queued=%d",
                len(self._queue),
            )
        return False

    def flush_pending(self) -> int:
        """Try to deliver queued messages in FIFO order."""
        if not self.enabled or not self._queue:
            return 0

        delivered = 0
        while self._queue:
            if not self._send_encoded(self._queue[0]):
                break
            self._queue.popleft()
            delivered += 1

        if delivered:
            LOG.info(
                "Delivered %d queued backend message(s); remaining=%d",
                delivered,
                len(self._queue),
            )
        return delivered


@dataclass
class ConnectionState:
    handle: int
    address: str
    address_type: int
    name: str
    notify_characteristic: Optional[int] = None
    notify_characteristic_uuid: Optional[str] = None
    notify_characteristic_properties: int = 0
    target_service: Optional[int] = None
    phase: str = "discover_service"


class BleCentral:
    """Single-xG26 BLE Central implemented through a Silicon Labs NCP."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        connector = bgapi.SerialConnector(
            args.serial_port,
            baudrate=args.baudrate,
            rtscts=not args.no_flow_control,
        )
        self.lib = bgapi.BGLib(connector, args.xapi)

        self.target_service_uuid = uuid_to_bgapi_bytes(args.service_uuid)
        self.target_notify_uuid = uuid_to_bgapi_bytes(args.notify_uuid)
        self.target_address = (
            normalize_address(args.address) if args.address is not None else None
        )

        self.backend = JsonLineBackend(args.backend_socket)

        self.booted = False
        self.scanning = False
        self.connecting = False
        self.connection: Optional[ConnectionState] = None
        self.pending_name = ""
        self.pending_address_type = 0
        self.boot_deadline = 0.0
        self.reconnect_after = 0.0

    def run(self) -> None:
        try:
            self.lib.open()
        except (OSError, bgapi.bglib.BGLibError) as exc:
            raise SystemExit(
                f"Cannot open BGM220 NCP at {self.args.serial_port}: {exc}\n"
                "Check the path, add the user to the dialout group, and ensure "
                "no other program is using the serial port."
            ) from exc

        LOG.info("Opened NCP serial port: %s", self.args.serial_port)
        self.boot_deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS

        try:
            self.lib.bt.system.reboot()
        except bgapi.bglib.CommandFailedError as exc:
            self.close()
            raise SystemExit(f"NCP reboot failed: 0x{exc.errorcode:04x}") from exc

        try:
            while True:
                now = time.monotonic()

                if not self.booted and now > self.boot_deadline:
                    raise SystemExit(
                        "NCP boot timeout. Check BGM220 firmware, serial baud rate, "
                        "RTS/CTS setting, and sl_bt.xapi version."
                    )

                if (
                    self.booted
                    and self.connection is None
                    and not self.connecting
                    and not self.scanning
                    and now >= self.reconnect_after
                ):
                    self.start_scan()

                if self.backend.enabled:
                    self.backend.flush_pending()

                event = self.lib.get_event(timeout=0.5)
                if event is not None:
                    self.dispatch(event)

        except KeyboardInterrupt:
            LOG.info("Stopped by user.")
        finally:
            self.close()

    def close(self) -> None:
        self.stop_scan()

        if self.connection is not None:
            try:
                self.lib.bt.connection.close(self.connection.handle)
            except Exception:
                pass
            self.connection = None

        self.backend.close()

        try:
            self.lib.close()
        except Exception:
            pass

        LOG.info("BLE Central closed.")

    def start_scan(self) -> None:
        if self.scanning or self.connection is not None or self.connecting:
            return

        try:
            self.lib.bt.scanner.set_parameters(
                SCAN_ACTIVE,
                self.args.scan_interval,
                self.args.scan_window,
            )
            self.lib.bt.scanner.start(PHY_1M, SCANNER_DISCOVER_OBSERVATION)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("Cannot start scanner: 0x%04x", exc.errorcode)
            self.reconnect_after = time.monotonic() + RECONNECT_DELAY_SECONDS
            return

        self.scanning = True
        target = self.target_address or f"name prefix '{self.args.name_prefix}'"
        LOG.info("Scanning for xG26 by %s...", target)

    def stop_scan(self) -> None:
        if not self.scanning:
            return

        try:
            self.lib.bt.scanner.stop()
        except Exception:
            pass
        self.scanning = False

    def dispatch(self, event: Any) -> None:
        if event == "bt_evt_system_boot":
            self.on_system_boot(event)
        elif event in (
            "bt_evt_scanner_legacy_advertisement_report",
            "bt_evt_scanner_scan_report",
        ):
            self.on_advertisement(event)
        elif event == "bt_evt_connection_opened":
            self.on_connection_opened(event)
        elif event == "bt_evt_connection_closed":
            self.on_connection_closed(event)
        elif event == "bt_evt_gatt_service":
            self.on_gatt_service(event)
        elif event == "bt_evt_gatt_characteristic":
            self.on_gatt_characteristic(event)
        elif event == "bt_evt_gatt_characteristic_value":
            self.on_gatt_characteristic_value(event)
        elif event == "bt_evt_gatt_procedure_completed":
            self.on_gatt_procedure_completed(event)

    def on_system_boot(self, event: Any) -> None:
        self.booted = True
        LOG.info(
            "BGM220 Bluetooth stack booted: %d.%d.%d build %d",
            event.major,
            event.minor,
            event.patch,
            event.build,
        )
        self.start_scan()

    def on_advertisement(self, event: Any) -> None:
        if not self.scanning or self.connecting or self.connection is not None:
            return

        try:
            address = normalize_address(event.address)
        except ValueError:
            return

        name = extract_advertised_name(event.data) or ""

        if self.target_address is not None:
            matches = address == self.target_address
        else:
            matches = bool(name) and name.startswith(self.args.name_prefix)

        if not matches:
            return

        LOG.info(
            "Found xG26 name=%r address=%s type=%d RSSI=%d dBm",
            name,
            address,
            event.address_type,
            event.rssi,
        )

        self.stop_scan()
        self.connecting = True
        self.pending_name = name or address
        self.pending_address_type = event.address_type

        try:
            self.lib.bt.connection.open(event.address, event.address_type, PHY_1M)
        except bgapi.bglib.CommandFailedError as exc:
            self.connecting = False
            LOG.error("connection.open failed: 0x%04x", exc.errorcode)
            self.reconnect_after = time.monotonic() + RECONNECT_DELAY_SECONDS

    def on_connection_opened(self, event: Any) -> None:
        address = normalize_address(event.address)
        self.connecting = False
        self.connection = ConnectionState(
            handle=event.connection,
            address=address,
            address_type=getattr(event, "address_type", self.pending_address_type),
            name=self.pending_name or address,
        )

        LOG.info(
            "Connected to xG26 name=%r address=%s handle=%d",
            self.connection.name,
            address,
            event.connection,
        )

        try:
            self.lib.bt.gatt.discover_primary_services(event.connection)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("Service discovery command failed: 0x%04x", exc.errorcode)
            self.disconnect_current()

    def on_gatt_service(self, event: Any) -> None:
        if self.connection is None or event.connection != self.connection.handle:
            return

        uuid_text = bgapi_uuid_to_text(event.uuid)
        LOG.debug(
            "Service handle=%s uuid=%s",
            event.service,
            uuid_text,
        )

        if bytes(event.uuid) == self.target_service_uuid:
            self.connection.target_service = event.service
            LOG.info(
                "Found target service uuid=%s handle=%s",
                uuid_text,
                event.service,
            )

    def on_gatt_characteristic(self, event: Any) -> None:
        if self.connection is None or event.connection != self.connection.handle:
            return

        uuid_text = bgapi_uuid_to_text(event.uuid)
        LOG.debug(
            "Characteristic handle=%s uuid=%s properties=0x%02x",
            event.characteristic,
            uuid_text,
            getattr(event, "properties", 0),
        )

        if bytes(event.uuid) == self.target_notify_uuid:
            properties = getattr(event, "properties", 0)
            if not (properties & GATT_PROPERTY_NOTIFY):
                LOG.error(
                    "Target characteristic uuid=%s exists but does not support Notify "
                    "(properties=0x%02x).",
                    uuid_text,
                    properties,
                )
                return

            self.connection.notify_characteristic = event.characteristic
            self.connection.notify_characteristic_uuid = uuid_text
            self.connection.notify_characteristic_properties = properties
            LOG.info(
                "Found notify characteristic uuid=%s handle=%s properties=0x%02x",
                uuid_text,
                event.characteristic,
                properties,
            )

    def on_gatt_procedure_completed(self, event: Any) -> None:
        connection = self.connection
        if connection is None or event.connection != connection.handle:
            return

        if event.result != 0:
            LOG.error(
                "GATT procedure failed in phase=%s result=0x%04x",
                connection.phase,
                event.result,
            )
            self.disconnect_current()
            return

        if connection.phase == "discover_service":
            if connection.target_service is None:
                LOG.error(
                    "Target service %s was not found on xG26.",
                    self.args.service_uuid,
                )
                self.disconnect_current()
                return

            connection.phase = "discover_characteristic"
            try:
                self.lib.bt.gatt.discover_characteristics(
                    connection.handle,
                    connection.target_service,
                )
            except bgapi.bglib.CommandFailedError as exc:
                LOG.error(
                    "Characteristic discovery failed for service handle=%s: 0x%04x",
                    connection.target_service,
                    exc.errorcode,
                )
                self.disconnect_current()

        elif connection.phase == "discover_characteristic":
            if connection.notify_characteristic is None:
                LOG.error(
                    "Notify characteristic %s was not found or does not support Notify.",
                    self.args.notify_uuid,
                )
                self.disconnect_current()
                return

            connection.phase = "enable_notifications"
            try:
                self.lib.bt.gatt.set_characteristic_notification(
                    connection.handle,
                    connection.notify_characteristic,
                    GATT_NOTIFICATION,
                )
            except bgapi.bglib.CommandFailedError as exc:
                LOG.error("Cannot enable notifications: 0x%04x", exc.errorcode)
                self.disconnect_current()

        elif connection.phase == "enable_notifications":
            connection.phase = "running"
            LOG.info(
                "Notifications enabled. Waiting for xG26 data on handle=%s.",
                connection.notify_characteristic,
            )

    def on_gatt_characteristic_value(self, event: Any) -> None:
        connection = self.connection
        if connection is None or event.connection != connection.handle:
            return

        opcode = event.att_opcode
        if opcode == ATT_READ_RESPONSE:
            LOG.debug("Read response: %s", bytes(event.value).hex())
            return

        if opcode not in (
            ATT_HANDLE_VALUE_NOTIFICATION,
            ATT_HANDLE_VALUE_INDICATION,
        ):
            return

        payload = bytes(event.value)
        parsed = parse_event_payload(payload)

        message: dict[str, Any] = {
            "schema_version": 1,
            "event": "ble_notification",
            "received_at": utc_now_iso(),
            "device": {
                "name": connection.name,
                "address": connection.address,
                "address_type": connection.address_type,
                "connection_handle": connection.handle,
            },
            "gatt": {
                "characteristic_handle": event.characteristic,
                "characteristic_uuid": connection.notify_characteristic_uuid,
                "att_opcode": opcode,
                "delivery": (
                    "indication"
                    if opcode == ATT_HANDLE_VALUE_INDICATION
                    else "notification"
                ),
            },
            "payload_hex": payload.hex(),
            "payload_length": len(payload),
            "parsed": parsed,
        }

        LOG.info(
            "Notify from %s: len=%d payload=%s parsed=%s",
            connection.address,
            len(payload),
            payload.hex(),
            parsed,
        )

        # stdout is the diagnostic ground truth and remains useful before the
        # backend teammate has implemented the socket server.
        print(json.dumps(message, ensure_ascii=False), flush=True)

        if self.backend.enabled and not self.backend.send(message):
            LOG.warning("Notification queued for backend retry; BLE remains active.")

        if opcode == ATT_HANDLE_VALUE_INDICATION:
            try:
                self.lib.bt.gatt.send_characteristic_confirmation(connection.handle)
            except bgapi.bglib.CommandFailedError as exc:
                LOG.error("Indication confirmation failed: 0x%04x", exc.errorcode)

    def on_connection_closed(self, event: Any) -> None:
        old = self.connection
        self.connection = None
        self.connecting = False

        if old is not None:
            LOG.warning(
                "xG26 disconnected: name=%r address=%s reason=0x%04x",
                old.name,
                old.address,
                event.reason,
            )
        else:
            LOG.warning("BLE connection closed: reason=0x%04x", event.reason)

        self.reconnect_after = time.monotonic() + RECONNECT_DELAY_SECONDS

    def disconnect_current(self) -> None:
        if self.connection is None:
            return
        try:
            self.lib.bt.connection.close(self.connection.handle)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.warning("connection.close failed: 0x%04x", exc.errorcode)


def build_argument_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description=(
            "Connect Raspberry Pi/BGM220 NCP to an EFR32xG26 peripheral, "
            "receive GATT notifications, and forward them to a backend."
        )
    )
    parser.add_argument(
        "serial_port",
        help=(
            "BGM220 serial port, preferably the stable "
            "/dev/serial/by-id/... path."
        ),
    )
    parser.add_argument(
        "--xapi",
        default=str(script_dir / "sl_bt.xapi"),
        help="Path to sl_bt.xapi matching the BGM220 NCP Bluetooth stack.",
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--no-flow-control",
        action="store_true",
        help="Disable serial RTS/CTS. Leave disabled only when required by the NCP setup.",
    )
    parser.add_argument(
        "--name-prefix",
        default="MyDevice",
        help="Connect to the first advertising name beginning with this prefix.",
    )
    parser.add_argument(
        "--address",
        help="Optional exact xG26 Bluetooth address; overrides --name-prefix.",
    )
    parser.add_argument(
        "--service-uuid",
        default="b5e00001-7a4b-4c6d-9e10-112233445566",
        type=normalize_uuid,
        help="128-bit UUID of the BreathSense event service.",
    )
    parser.add_argument(
        "--notify-uuid",
        default="b5e00002-7a4b-4c6d-9e10-112233445566",
        type=normalize_uuid,
        help="UUID of the xG26 event characteristic (default: BreathSense event UUID).",
    )
    parser.add_argument(
        "--backend-socket",
        help="Optional Unix stream socket exposed by the backend.",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=80,
        help="BLE scan interval in 0.625 ms units (default 80 = 50 ms).",
    )
    parser.add_argument(
        "--scan-window",
        type=int,
        default=40,
        help="BLE scan window in 0.625 ms units (default 40 = 25 ms).",
    )
    parser.add_argument(
        "-l",
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()

    if args.scan_window > args.scan_interval:
        raise SystemExit("--scan-window must be less than or equal to --scan-interval.")

    if not os.path.isfile(args.xapi):
        raise SystemExit(
            f"sl_bt.xapi not found: {args.xapi}\n"
            "Copy the file matching the BGM220 NCP SDK version, or pass --xapi."
        )

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    BleCentral(args).run()


if __name__ == "__main__":
    main()
