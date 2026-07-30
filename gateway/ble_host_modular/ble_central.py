"""Single-node BLE Central logic for Raspberry Pi + BGM220 NCP."""

from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any, Optional

import bgapi

from advertisement import extract_advertised_name
from backend_client import JsonLineBackend
from constants import (
    ATT_HANDLE_VALUE_INDICATION,
    ATT_HANDLE_VALUE_NOTIFICATION,
    ATT_READ_RESPONSE,
    BOOT_TIMEOUT_SECONDS,
    GATT_NOTIFICATION,
    GATT_PROPERTY_NOTIFY,
    PHY_1M,
    RECONNECT_DELAY_SECONDS,
    SCAN_ACTIVE,
    SCANNER_DISCOVER_OBSERVATION,
)
from models import ConnectionState
from payload_parser import (
    parse_cough_payload,
    parse_environment_payload,
)
from utils import (
    bgapi_uuid_to_text,
    normalize_address,
    utc_now_iso,
    uuid_to_bgapi_bytes,
)

LOG = logging.getLogger("breathsense.ble_central")


class BleCentral:
    """Single-xG26 Central subscribing to two notify characteristics."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args

        connector = bgapi.SerialConnector(
            args.serial_port,
            baudrate=args.baudrate,
            rtscts=not args.no_flow_control,
        )
        self.lib = bgapi.BGLib(connector, args.xapi)

        self.target_service_uuid = uuid_to_bgapi_bytes(args.service_uuid)
        self.target_cough_uuid = uuid_to_bgapi_bytes(args.cough_uuid)
        self.target_environment_uuid = uuid_to_bgapi_bytes(
            args.environment_uuid
        )

        self.target_address = (
            normalize_address(args.address)
            if args.address is not None
            else None
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
                "Check the serial path, dialout membership, and whether "
                "another program is using the port."
            ) from exc

        LOG.info("Opened NCP serial port: %s", self.args.serial_port)
        self.boot_deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS

        try:
            self.lib.bt.system.reboot()
        except bgapi.bglib.CommandFailedError as exc:
            self.close()
            raise SystemExit(
                f"NCP reboot failed: 0x{exc.errorcode:04x}"
            ) from exc

        try:
            while True:
                now = time.monotonic()

                if not self.booted and now > self.boot_deadline:
                    raise SystemExit(
                        "NCP boot timeout. Check BGM220 firmware, baud rate, "
                        "RTS/CTS, and sl_bt.xapi."
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
        if self.scanning or self.connecting or self.connection is not None:
            return

        try:
            self.lib.bt.scanner.set_parameters(
                SCAN_ACTIVE,
                self.args.scan_interval,
                self.args.scan_window,
            )
            self.lib.bt.scanner.start(
                PHY_1M,
                SCANNER_DISCOVER_OBSERVATION,
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("Cannot start scanner: 0x%04x", exc.errorcode)
            self.reconnect_after = (
                time.monotonic() + RECONNECT_DELAY_SECONDS
            )
            return

        self.scanning = True
        target = (
            self.target_address
            or f"name prefix '{self.args.name_prefix}'"
        )
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
            matches = bool(name) and name.startswith(
                self.args.name_prefix
            )

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
            self.lib.bt.connection.open(
                event.address,
                event.address_type,
                PHY_1M,
            )
        except bgapi.bglib.CommandFailedError as exc:
            self.connecting = False
            LOG.error("connection.open failed: 0x%04x", exc.errorcode)
            self.reconnect_after = (
                time.monotonic() + RECONNECT_DELAY_SECONDS
            )

    def on_connection_opened(self, event: Any) -> None:
        address = normalize_address(event.address)
        self.connecting = False

        self.connection = ConnectionState(
            handle=event.connection,
            address=address,
            address_type=getattr(
                event,
                "address_type",
                self.pending_address_type,
            ),
            name=self.pending_name or address,
        )

        LOG.info(
            "Connected to xG26 name=%r address=%s handle=%d",
            self.connection.name,
            address,
            event.connection,
        )

        try:
            self.lib.bt.gatt.discover_primary_services(
                event.connection
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error(
                "Service discovery command failed: 0x%04x",
                exc.errorcode,
            )
            self.disconnect_current()

    def on_gatt_service(self, event: Any) -> None:
        connection = self.connection

        if (
            connection is None
            or event.connection != connection.handle
        ):
            return

        uuid_text = bgapi_uuid_to_text(event.uuid)

        LOG.debug(
            "Service handle=%s uuid=%s",
            event.service,
            uuid_text,
        )

        if bytes(event.uuid) == self.target_service_uuid:
            connection.target_service = event.service

            LOG.info(
                "Found target service uuid=%s handle=%s",
                uuid_text,
                event.service,
            )

    def on_gatt_characteristic(self, event: Any) -> None:
        connection = self.connection

        if (
            connection is None
            or event.connection != connection.handle
        ):
            return

        uuid_text = bgapi_uuid_to_text(event.uuid)
        properties = getattr(event, "properties", 0)

        LOG.debug(
            "Characteristic handle=%s uuid=%s properties=0x%02x",
            event.characteristic,
            uuid_text,
            properties,
        )

        if bytes(event.uuid) == self.target_cough_uuid:
            if not (properties & GATT_PROPERTY_NOTIFY):
                LOG.error(
                    "Cough characteristic uuid=%s does not support Notify "
                    "(properties=0x%02x).",
                    uuid_text,
                    properties,
                )
                return

            connection.cough_characteristic = event.characteristic
            connection.cough_characteristic_uuid = uuid_text
            connection.cough_characteristic_properties = properties

            LOG.info(
                "Found cough characteristic uuid=%s handle=%s "
                "properties=0x%02x",
                uuid_text,
                event.characteristic,
                properties,
            )

        elif bytes(event.uuid) == self.target_environment_uuid:
            if not (properties & GATT_PROPERTY_NOTIFY):
                LOG.error(
                    "Environment characteristic uuid=%s does not support "
                    "Notify (properties=0x%02x).",
                    uuid_text,
                    properties,
                )
                return

            connection.environment_characteristic = event.characteristic
            connection.environment_characteristic_uuid = uuid_text
            connection.environment_characteristic_properties = properties

            LOG.info(
                "Found environment characteristic uuid=%s handle=%s "
                "properties=0x%02x",
                uuid_text,
                event.characteristic,
                properties,
            )

    def on_gatt_procedure_completed(self, event: Any) -> None:
        connection = self.connection

        if (
            connection is None
            or event.connection != connection.handle
        ):
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
            self._finish_service_discovery(connection)

        elif connection.phase == "discover_characteristics":
            self._finish_characteristic_discovery(connection)

        elif connection.phase == "enable_cough_notifications":
            self._enable_environment_notifications(connection)

        elif connection.phase == "enable_environment_notifications":
            connection.phase = "running"

            LOG.info(
                "Notifications enabled for cough handle=%s and "
                "environment handle=%s.",
                connection.cough_characteristic,
                connection.environment_characteristic,
            )

    def _finish_service_discovery(
        self,
        connection: ConnectionState,
    ) -> None:
        if connection.target_service is None:
            LOG.error(
                "Target service %s was not found on xG26.",
                self.args.service_uuid,
            )
            self.disconnect_current()
            return

        connection.phase = "discover_characteristics"

        try:
            self.lib.bt.gatt.discover_characteristics(
                connection.handle,
                connection.target_service,
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error(
                "Characteristic discovery failed for service "
                "handle=%s: 0x%04x",
                connection.target_service,
                exc.errorcode,
            )
            self.disconnect_current()

    def _finish_characteristic_discovery(
        self,
        connection: ConnectionState,
    ) -> None:
        missing = []

        if connection.cough_characteristic is None:
            missing.append(self.args.cough_uuid)

        if connection.environment_characteristic is None:
            missing.append(self.args.environment_uuid)

        if missing:
            LOG.error(
                "Required notify characteristic(s) not found or not "
                "notifiable: %s",
                ", ".join(missing),
            )
            self.disconnect_current()
            return

        connection.phase = "enable_cough_notifications"

        try:
            self.lib.bt.gatt.set_characteristic_notification(
                connection.handle,
                connection.cough_characteristic,
                GATT_NOTIFICATION,
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error(
                "Cannot enable cough notifications: 0x%04x",
                exc.errorcode,
            )
            self.disconnect_current()

    def _enable_environment_notifications(
        self,
        connection: ConnectionState,
    ) -> None:
        connection.phase = "enable_environment_notifications"

        try:
            self.lib.bt.gatt.set_characteristic_notification(
                connection.handle,
                connection.environment_characteristic,
                GATT_NOTIFICATION,
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error(
                "Cannot enable environment notifications: 0x%04x",
                exc.errorcode,
            )
            self.disconnect_current()

    def on_gatt_characteristic_value(self, event: Any) -> None:
        connection = self.connection

        if (
            connection is None
            or event.connection != connection.handle
        ):
            return

        opcode = event.att_opcode

        if opcode == ATT_READ_RESPONSE:
            LOG.debug(
                "Read response on handle=%s: %s",
                event.characteristic,
                bytes(event.value).hex(),
            )
            return

        if opcode not in (
            ATT_HANDLE_VALUE_NOTIFICATION,
            ATT_HANDLE_VALUE_INDICATION,
        ):
            return

        payload = bytes(event.value)

        if event.characteristic == connection.cough_characteristic:
            event_type = "cough_event"
            characteristic_uuid = (
                connection.cough_characteristic_uuid
            )
            parsed = parse_cough_payload(payload)

        elif (
            event.characteristic
            == connection.environment_characteristic
        ):
            event_type = "environment_data"
            characteristic_uuid = (
                connection.environment_characteristic_uuid
            )
            parsed = parse_environment_payload(payload)

        else:
            LOG.warning(
                "Notification from unknown characteristic handle=%s "
                "payload=%s",
                event.characteristic,
                payload.hex(),
            )
            return

        message: dict[str, Any] = {
            "schema_version": 1,
            "event": event_type,
            "received_at": utc_now_iso(),
            "device": {
                "name": connection.name,
                "address": connection.address,
                "address_type": connection.address_type,
                "connection_handle": connection.handle,
            },
            "gatt": {
                "characteristic_handle": event.characteristic,
                "characteristic_uuid": characteristic_uuid,
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
            "%s from %s: len=%d payload=%s parsed=%s",
            event_type,
            connection.address,
            len(payload),
            payload.hex(),
            parsed,
        )

        print(
            json.dumps(message, ensure_ascii=False),
            flush=True,
        )

        if self.backend.enabled and not self.backend.send(message):
            LOG.warning(
                "Notification queued for backend retry; BLE remains active."
            )

        if opcode == ATT_HANDLE_VALUE_INDICATION:
            try:
                self.lib.bt.gatt.send_characteristic_confirmation(
                    connection.handle
                )
            except bgapi.bglib.CommandFailedError as exc:
                LOG.error(
                    "Indication confirmation failed: 0x%04x",
                    exc.errorcode,
                )

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
            LOG.warning(
                "BLE connection closed: reason=0x%04x",
                event.reason,
            )

        self.reconnect_after = (
            time.monotonic() + RECONNECT_DELAY_SECONDS
        )

    def disconnect_current(self) -> None:
        if self.connection is None:
            return

        try:
            self.lib.bt.connection.close(
                self.connection.handle
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.warning(
                "connection.close failed: 0x%04x",
                exc.errorcode,
            )
