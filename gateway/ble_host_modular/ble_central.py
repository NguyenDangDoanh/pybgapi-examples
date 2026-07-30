"""Multi-node BLE Central logic for Raspberry Pi + BGM220 NCP."""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from typing import Any

import bgapi

from advertisement import extract_advertised_name
from backend_client import JsonLineBackend
from constants import (
    ATT_HANDLE_VALUE_INDICATION,
    ATT_HANDLE_VALUE_NOTIFICATION,
    ATT_READ_RESPONSE,
    BOOT_TIMEOUT_SECONDS,
    CLOSE_TIMEOUT_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    FAILED_NODE_RETRY_SECONDS,
    GATT_PROCEDURE_TIMEOUT_SECONDS,
    GATT_NOTIFICATION,
    GATT_PROPERTY_NOTIFY,
    PHY_1M,
    RECONNECT_DELAY_SECONDS,
    SCAN_ACTIVE,
    SCANNER_DISCOVER_OBSERVATION,
)
from models import ConnectionState, PendingNode
from payload_parser import parse_cough_payload, parse_environment_payload
from utils import (
    bgapi_uuid_to_text,
    normalize_address,
    resolve_event_timestamp,
    utc_now_iso,
    uuid_to_bgapi_bytes,
)

LOG = logging.getLogger("breathsense.ble_central")


class BleCentral:
    """Connect to several xG26 nodes and subscribe independently to each."""

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
        self.target_environment_uuid = uuid_to_bgapi_bytes(args.environment_uuid)
        self.target_address = (
            normalize_address(args.address) if args.address is not None else None
        )

        self.backend = JsonLineBackend(args.backend_socket)
        self.booted = False
        self.scanning = False
        self.pending_node: PendingNode | None = None
        self.connections: dict[int, ConnectionState] = {}
        self.connection_by_address: dict[str, int] = {}
        self.retry_after_by_address: dict[str, float] = {}
        self.boot_deadline = 0.0
        self.scan_retry_after = 0.0
        self.session_id = str(uuid.uuid4())
        self.sequence = 0

    @property
    def connecting(self) -> bool:
        return self.pending_node is not None

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
        LOG.info("BLE host session=%s max_connections=%d", self.session_id, self.args.max_connections)
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
                        "NCP boot timeout. Check BGM220 firmware, baud rate, "
                        "RTS/CTS, and sl_bt.xapi."
                    )

                self._check_timeouts(now)

                if self._has_connection_capacity() and not self.connecting and not self.scanning:
                    if now >= self.scan_retry_after:
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
        for handle in list(self.connections):
            try:
                self.lib.bt.connection.close(handle)
            except Exception:
                pass
        self.connections.clear()
        self.connection_by_address.clear()
        self.pending_node = None
        self.backend.close()
        try:
            self.lib.close()
        except Exception:
            pass
        LOG.info("BLE Central closed.")

    def _has_connection_capacity(self) -> bool:
        return len(self.connections) < self.args.max_connections

    @staticmethod
    def _set_phase_deadline(state: ConnectionState) -> None:
        state.phase_deadline = time.monotonic() + GATT_PROCEDURE_TIMEOUT_SECONDS

    def _check_timeouts(self, now: float) -> None:
        pending = self.pending_node
        if pending is not None and now - pending.started_at >= CONNECT_TIMEOUT_SECONDS:
            LOG.error("Connection open timeout for %s", pending.address)
            self.retry_after_by_address[pending.address] = (
                now + FAILED_NODE_RETRY_SECONDS
            )
            self.pending_node = None
            self.scan_retry_after = now + RECONNECT_DELAY_SECONDS

        for state in list(self.connections.values()):
            if not state.phase_deadline or now < state.phase_deadline:
                continue
            if state.phase == "closing":
                LOG.error(
                    "[%s] Connection close timeout; forgetting stale handle=%d",
                    state.address,
                    state.handle,
                )
                if state.status_reported:
                    self._emit_status(state, "disconnected")
                self._forget_connection(state)
                continue
            if state.phase != "running":
                LOG.error(
                    "[%s] GATT timeout phase=%s; disconnecting",
                    state.address,
                    state.phase,
                )
                self.disconnect_connection(state.handle)

    def _forget_connection(self, state: ConnectionState) -> None:
        self.connections.pop(state.handle, None)
        self.connection_by_address.pop(state.address, None)
        self.retry_after_by_address[state.address] = (
            time.monotonic() + FAILED_NODE_RETRY_SECONDS
        )
        self.scan_retry_after = time.monotonic() + RECONNECT_DELAY_SECONDS

    def start_scan(self) -> None:
        if self.scanning or self.connecting or not self._has_connection_capacity():
            return
        try:
            self.lib.bt.scanner.set_parameters(
                SCAN_ACTIVE, self.args.scan_interval, self.args.scan_window
            )
            self.lib.bt.scanner.start(PHY_1M, SCANNER_DISCOVER_OBSERVATION)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("Cannot start scanner: 0x%04x", exc.errorcode)
            self.scan_retry_after = time.monotonic() + RECONNECT_DELAY_SECONDS
            return

        self.scanning = True
        target = self.target_address or f"name prefix '{self.args.name_prefix}'"
        LOG.info(
            "Scanning for xG26 by %s (%d/%d connected)...",
            target,
            len(self.connections),
            self.args.max_connections,
        )

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
        if not self.scanning or self.connecting or not self._has_connection_capacity():
            return
        try:
            address = normalize_address(event.address)
        except ValueError:
            return

        name = extract_advertised_name(event.data) or ""
        matches = (
            address == self.target_address
            if self.target_address is not None
            else bool(name) and name.startswith(self.args.name_prefix)
        )
        if not matches or address in self.connection_by_address:
            return
        if time.monotonic() < self.retry_after_by_address.get(address, 0.0):
            return

        LOG.info(
            "Found xG26 name=%r address=%s type=%d RSSI=%d dBm",
            name,
            address,
            event.address_type,
            event.rssi,
        )
        self.stop_scan()
        self.pending_node = PendingNode(
            address=address,
            address_type=event.address_type,
            name=name or address,
            started_at=time.monotonic(),
        )
        try:
            self.lib.bt.connection.open(event.address, event.address_type, PHY_1M)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("connection.open failed for %s: 0x%04x", address, exc.errorcode)
            self.retry_after_by_address[address] = time.monotonic() + FAILED_NODE_RETRY_SECONDS
            self.pending_node = None
            self.scan_retry_after = time.monotonic() + RECONNECT_DELAY_SECONDS

    def on_connection_opened(self, event: Any) -> None:
        address = normalize_address(event.address)
        pending = self.pending_node
        self.pending_node = None
        if not self._has_connection_capacity():
            LOG.warning("Connection capacity exceeded; closing handle=%d", event.connection)
            try:
                self.lib.bt.connection.close(event.connection)
            except Exception:
                pass
            return

        state = ConnectionState(
            handle=event.connection,
            address=address,
            address_type=getattr(event, "address_type", pending.address_type if pending else 0),
            name=(pending.name if pending and pending.address == address else address),
        )
        self.connections[state.handle] = state
        self.connection_by_address[address] = state.handle
        self.retry_after_by_address.pop(address, None)
        LOG.info(
            "Connected to xG26 name=%r address=%s handle=%d (%d/%d)",
            state.name,
            address,
            state.handle,
            len(self.connections),
            self.args.max_connections,
        )
        self._set_phase_deadline(state)
        try:
            self.lib.bt.gatt.discover_primary_services(state.handle)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("Service discovery command failed for %s: 0x%04x", address, exc.errorcode)
            self.disconnect_connection(state.handle)

    def _state_for_event(self, event: Any) -> ConnectionState | None:
        return self.connections.get(getattr(event, "connection", -1))

    def on_gatt_service(self, event: Any) -> None:
        state = self._state_for_event(event)
        if state is None:
            return
        uuid_text = bgapi_uuid_to_text(event.uuid)
        LOG.debug("[%s] Service handle=%s uuid=%s", state.address, event.service, uuid_text)
        if bytes(event.uuid) == self.target_service_uuid:
            state.target_service = event.service
            LOG.info("[%s] Found target service uuid=%s handle=%s", state.address, uuid_text, event.service)

    def on_gatt_characteristic(self, event: Any) -> None:
        state = self._state_for_event(event)
        if state is None:
            return
        uuid_text = bgapi_uuid_to_text(event.uuid)
        properties = getattr(event, "properties", 0)
        LOG.debug(
            "[%s] Characteristic handle=%s uuid=%s properties=0x%02x",
            state.address,
            event.characteristic,
            uuid_text,
            properties,
        )
        if bytes(event.uuid) == self.target_cough_uuid:
            if not properties & GATT_PROPERTY_NOTIFY:
                LOG.error("[%s] Cough characteristic does not support Notify", state.address)
                return
            state.cough_characteristic = event.characteristic
            state.cough_characteristic_uuid = uuid_text
            state.cough_characteristic_properties = properties
        elif bytes(event.uuid) == self.target_environment_uuid:
            if not properties & GATT_PROPERTY_NOTIFY:
                LOG.error("[%s] Environment characteristic does not support Notify", state.address)
                return
            state.environment_characteristic = event.characteristic
            state.environment_characteristic_uuid = uuid_text
            state.environment_characteristic_properties = properties

    def on_gatt_procedure_completed(self, event: Any) -> None:
        state = self._state_for_event(event)
        if state is None:
            return
        if event.result != 0:
            LOG.error(
                "[%s] GATT procedure failed phase=%s result=0x%04x",
                state.address,
                state.phase,
                event.result,
            )
            self.disconnect_connection(state.handle)
            return

        if state.phase == "discover_service":
            self._finish_service_discovery(state)
        elif state.phase == "discover_characteristics":
            self._finish_characteristic_discovery(state)
        elif state.phase == "enable_cough_notifications":
            self._enable_environment_notifications(state)
        elif state.phase == "enable_environment_notifications":
            state.phase = "running"
            state.phase_deadline = 0.0
            LOG.info(
                "[%s] Notifications enabled: cough=%s environment=%s",
                state.address,
                state.cough_characteristic,
                state.environment_characteristic,
            )
            self._emit_status(state, "connected")
            state.status_reported = True
            self.start_scan()

    def _finish_service_discovery(self, state: ConnectionState) -> None:
        if state.target_service is None:
            LOG.error("[%s] Target service %s not found", state.address, self.args.service_uuid)
            self.disconnect_connection(state.handle)
            return
        state.phase = "discover_characteristics"
        self._set_phase_deadline(state)
        try:
            self.lib.bt.gatt.discover_characteristics(state.handle, state.target_service)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("[%s] Characteristic discovery failed: 0x%04x", state.address, exc.errorcode)
            self.disconnect_connection(state.handle)

    def _finish_characteristic_discovery(self, state: ConnectionState) -> None:
        missing: list[str] = []
        if state.cough_characteristic is None:
            missing.append(self.args.cough_uuid)
        if state.environment_characteristic is None:
            missing.append(self.args.environment_uuid)
        if missing:
            LOG.error("[%s] Missing required notify characteristic(s): %s", state.address, ", ".join(missing))
            self.disconnect_connection(state.handle)
            return
        state.phase = "enable_cough_notifications"
        self._set_phase_deadline(state)
        try:
            self.lib.bt.gatt.set_characteristic_notification(
                state.handle, state.cough_characteristic, GATT_NOTIFICATION
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("[%s] Cannot enable cough notifications: 0x%04x", state.address, exc.errorcode)
            self.disconnect_connection(state.handle)

    def _enable_environment_notifications(self, state: ConnectionState) -> None:
        state.phase = "enable_environment_notifications"
        self._set_phase_deadline(state)
        try:
            self.lib.bt.gatt.set_characteristic_notification(
                state.handle, state.environment_characteristic, GATT_NOTIFICATION
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("[%s] Cannot enable environment notifications: 0x%04x", state.address, exc.errorcode)
            self.disconnect_connection(state.handle)

    def _next_envelope(self, state: ConnectionState, event_name: str, received_at: str) -> dict[str, Any]:
        self.sequence += 1
        return {
            "schema_version": 1,
            "message_id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "sequence": self.sequence,
            "event": event_name,
            "received_at": received_at,
            "device": {
                "device_id": state.address,
                "name": state.name,
                "address": state.address,
                "address_type": state.address_type,
                "connection_handle": state.handle,
            },
        }

    def _emit_status(self, state: ConnectionState, status: str, reason: int | None = None) -> None:
        received_at = utc_now_iso()
        message = self._next_envelope(state, "status", received_at)
        message.update({"status": status, "event_ts": received_at})
        if reason is not None:
            message["reason"] = reason
        self._publish(message)

    def on_gatt_characteristic_value(self, event: Any) -> None:
        state = self._state_for_event(event)
        if state is None:
            LOG.warning("Value event for unknown connection=%s", getattr(event, "connection", None))
            return
        opcode = event.att_opcode
        if opcode == ATT_READ_RESPONSE:
            LOG.debug("[%s] Read response handle=%s: %s", state.address, event.characteristic, bytes(event.value).hex())
            return
        if opcode not in (ATT_HANDLE_VALUE_NOTIFICATION, ATT_HANDLE_VALUE_INDICATION):
            return

        payload = bytes(event.value)
        received_at = utc_now_iso()
        if event.characteristic == state.cough_characteristic:
            event_name = "cough_event"
            characteristic_uuid = state.cough_characteristic_uuid
            parsed = parse_cough_payload(payload)
            if parsed is not None:
                event_ts, timestamp_source = resolve_event_timestamp(
                    parsed.get("event_timestamp"), received_at
                )
                parsed["event_timestamp_iso"] = event_ts
                parsed["timestamp_source"] = timestamp_source
            else:
                event_ts = received_at
        elif event.characteristic == state.environment_characteristic:
            event_name = "environment_data"
            characteristic_uuid = state.environment_characteristic_uuid
            parsed = parse_environment_payload(payload)
            event_ts = received_at
        else:
            LOG.warning(
                "[%s] Notification from unknown characteristic handle=%s payload=%s",
                state.address,
                event.characteristic,
                payload.hex(),
            )
            self._confirm_indication_if_needed(state, opcode)
            return

        if parsed is None:
            LOG.error(
                "[%s] Invalid %s payload length=%d payload=%s",
                state.address,
                event_name,
                len(payload),
                payload.hex(),
            )
            self._confirm_indication_if_needed(state, opcode)
            return

        message = self._next_envelope(state, event_name, received_at)
        message.update(
            {
                "event_ts": event_ts,
                "gatt": {
                    "characteristic_handle": event.characteristic,
                    "characteristic_uuid": characteristic_uuid,
                    "att_opcode": opcode,
                    "delivery": "indication" if opcode == ATT_HANDLE_VALUE_INDICATION else "notification",
                },
                "payload_hex": payload.hex(),
                "payload_length": len(payload),
                "parsed": parsed,
            }
        )
        LOG.info(
            "%s from %s: len=%d payload=%s parsed=%s",
            event_name,
            state.address,
            len(payload),
            payload.hex(),
            parsed,
        )
        self._publish(message)
        self._confirm_indication_if_needed(state, opcode)

    def _publish(self, message: dict[str, Any]) -> None:
        print(json.dumps(message, ensure_ascii=False), flush=True)
        if self.backend.enabled and not self.backend.send(message):
            LOG.warning("Message queued for backend retry; BLE remains active.")

    def _confirm_indication_if_needed(self, state: ConnectionState, opcode: int) -> None:
        if opcode != ATT_HANDLE_VALUE_INDICATION:
            return
        try:
            self.lib.bt.gatt.send_characteristic_confirmation(state.handle)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("[%s] Indication confirmation failed: 0x%04x", state.address, exc.errorcode)

    def on_connection_closed(self, event: Any) -> None:
        state = self.connections.get(event.connection)
        if state is not None:
            LOG.warning(
                "xG26 disconnected: name=%r address=%s handle=%d reason=0x%04x",
                state.name,
                state.address,
                state.handle,
                event.reason,
            )
            if state.status_reported:
                self._emit_status(state, "disconnected", event.reason)
            self._forget_connection(state)
        else:
            # A close without a registered state can belong to a failed or
            # timed-out open attempt.
            self.pending_node = None
            LOG.warning(
                "BLE connection closed: handle=%s reason=0x%04x",
                event.connection,
                event.reason,
            )
            self.scan_retry_after = time.monotonic() + RECONNECT_DELAY_SECONDS

    def disconnect_connection(self, handle: int) -> None:
        state = self.connections.get(handle)
        if state is None or state.phase == "closing":
            return
        state.phase = "closing"
        state.phase_deadline = time.monotonic() + CLOSE_TIMEOUT_SECONDS
        try:
            self.lib.bt.connection.close(handle)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.warning("[%s] connection.close failed: 0x%04x", state.address, exc.errorcode)
            if state.status_reported:
                self._emit_status(state, "disconnected")
            self._forget_connection(state)
