"""Multi-node BLE Central logic for Raspberry Pi + BGM220 NCP."""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import bgapi
from bgapi.connector import ConnectorException

from advertisement import extract_advertised_name
from backend_client import JsonLineBackend
from constants import (
    ATT_HANDLE_VALUE_INDICATION,
    ATT_HANDLE_VALUE_NOTIFICATION,
    ATT_READ_RESPONSE,
    BOOT_TIMEOUT_SECONDS,
    CLOSE_TIMEOUT_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    DEVICE_STATUS_HEARTBEAT_SECONDS,
    FAILED_NODE_RETRY_SECONDS,
    GATT_PROCEDURE_TIMEOUT_SECONDS,
    GATT_NOTIFICATION,
    GATT_PROPERTY_NOTIFY,
    GATT_PROPERTY_WRITE,
    NCP_HEALTH_CHECK_SECONDS,
    PHY_1M,
    RECONNECT_DELAY_SECONDS,
    SCAN_ACTIVE,
    SCAN_REJECTION_LOG_INTERVAL_SECONDS,
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


class NcpTransportLost(RuntimeError):
    """Raised when the BGM220 serial/BGAPI transport is unavailable."""


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
        self.target_time_uuid = uuid_to_bgapi_bytes(args.time_uuid)
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
        self.utc_sync_date = datetime.now(timezone.utc).date()
        self.session_id = str(uuid.uuid4())
        self.sequence = 0
        self._closed = False
        self._advertisement_debug_after: dict[tuple[str, str], float] = {}
        self._next_ncp_health_check_at = 0.0

    @property
    def connecting(self) -> bool:
        return self.pending_node is not None

    def run(self) -> None:
        try:
            self.lib.open()
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self.close()
            raise NcpTransportLost(
                f"Cannot open BGM220 NCP at {self.args.serial_port}: {exc}"
            ) from exc

        requested_port = str(self.args.serial_port)
        LOG.info(
            "Opened NCP serial port requested=%s resolved=%s baud=%d "
            "rtscts=%s session=%s",
            requested_port,
            os.path.realpath(requested_port),
            getattr(self.args, "baudrate", 115200),
            (
                "enabled"
                if not getattr(self.args, "no_flow_control", False)
                else "disabled"
            ),
            self.session_id,
        )
        LOG.info(
            "BLE host session=%s max_connections=%d",
            self.session_id,
            self.args.max_connections,
        )
        self.boot_deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS

        try:
            try:
                self.lib.bt.system.reboot()
            except bgapi.bglib.CommandFailedError as exc:
                if not self._ncp_transport_alive():
                    raise NcpTransportLost(
                        "BGM220 NCP transport lost during reboot"
                    ) from exc
                raise SystemExit(
                    f"NCP reboot failed: 0x{exc.errorcode:04x}"
                ) from exc
            except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
                self._raise_transport_lost("system.reboot", exc)

            while True:
                now = time.monotonic()
                if not self._ncp_transport_alive():
                    LOG.error("BGM220 NCP transport lost")
                    raise NcpTransportLost("BGM220 NCP transport lost")
                if not self.booted and now > self.boot_deadline:
                    raise NcpTransportLost(
                        "NCP boot timeout. Check BGM220 firmware, baud rate, "
                        "RTS/CTS, and sl_bt.xapi."
                    )

                self._check_timeouts(now)
                self._check_ncp_health(now)
                self._check_status_heartbeats(now)
                self._check_daily_time_sync()

                if (
                    self.booted
                    and self._has_connection_capacity()
                    and not self.connecting
                    and not self.scanning
                ):
                    if now >= self.scan_retry_after:
                        self.start_scan()

                if self.backend.enabled:
                    self.backend.flush_pending()

                try:
                    event = self.lib.get_event(timeout=0.5)
                except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
                    self._raise_transport_lost("event read", exc)
                if event is not None:
                    self.dispatch(event)
        except KeyboardInterrupt:
            LOG.info("Stopped by user.")
        finally:
            self.close()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True

        try:
            self.stop_scan(best_effort=True)
        except Exception:
            LOG.debug("Scanner cleanup failed", exc_info=True)

        connections = getattr(self, "connections", {})
        for state in list(connections.values()):
            if state.status_reported:
                try:
                    self._emit_status(state, "disconnected")
                except Exception:
                    LOG.exception(
                        "[%s] Failed to publish disconnected status during cleanup",
                        state.address,
                    )
                finally:
                    state.status_reported = False
        for handle in list(connections):
            try:
                self.lib.bt.connection.close(handle)
            except Exception:
                pass
        connections.clear()
        connection_by_address = getattr(self, "connection_by_address", None)
        if connection_by_address is not None:
            connection_by_address.clear()
        if hasattr(self, "pending_node"):
            self.pending_node = None
        try:
            backend = getattr(self, "backend", None)
            if backend is not None:
                backend.close()
        except Exception:
            LOG.debug("Backend cleanup failed", exc_info=True)
        try:
            library = getattr(self, "lib", None)
            if library is not None:
                library.close()
        except Exception:
            pass
        LOG.info("BLE Central closed.")

    def _ncp_transport_alive(self) -> bool:
        """Return whether the BGAPI reader thread is still running."""
        handler = getattr(getattr(self, "lib", None), "conn_handler", None)
        if handler is None:
            return False
        try:
            return bool(handler.is_alive())
        except Exception:
            return False

    def _check_ncp_health(self, now: float) -> None:
        """Actively verify command/response health, not only reader liveness."""
        if not self.booted or now < getattr(self, "_next_ncp_health_check_at", 0.0):
            return
        try:
            self.lib.bt.system.hello()
        except Exception as exc:
            self._raise_transport_lost("system.hello health check", exc)
        self._next_ncp_health_check_at = now + NCP_HEALTH_CHECK_SECONDS

    @staticmethod
    def _raise_transport_lost(operation: str, exc: BaseException) -> None:
        """Normalize a failed BGAPI session into one supervisor signal."""
        raise NcpTransportLost(
            f"BGM220 NCP transport failed during {operation}: {exc}"
        ) from exc

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
            if state.phase.startswith("sync_time"):
                LOG.error(
                    "[%s] Time synchronization timeout; disconnecting only this node",
                    state.address,
                )
                self.disconnect_connection(state.handle)
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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost("scanner.start", exc)

        self.scanning = True
        target = self.target_address or f"name prefix '{self.args.name_prefix}'"
        LOG.info(
            "Scanning for xG26 by %s (%d/%d connected)...",
            target,
            len(self.connections),
            self.args.max_connections,
        )

    def stop_scan(self, *, best_effort: bool = False) -> None:
        if not self.scanning:
            return
        try:
            self.lib.bt.scanner.stop()
        except bgapi.bglib.CommandFailedError as exc:
            LOG.warning("Cannot stop scanner: 0x%04x", exc.errorcode)
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            if best_effort:
                LOG.debug("Scanner cleanup failed: %s", exc)
            else:
                self.scanning = False
                self._raise_transport_lost("scanner.stop", exc)
        finally:
            self.scanning = False

    def _debug_rejected_advertisement(
        self,
        address: str,
        reason: str,
        event: Any,
        name: str = "",
        detail: str = "",
    ) -> None:
        """Rate-limit DEBUG diagnostics for scanner filtering decisions."""
        if not LOG.isEnabledFor(logging.DEBUG):
            return
        now = time.monotonic()
        key = (address, reason)
        if now < self._advertisement_debug_after.get(key, 0.0):
            return
        self._advertisement_debug_after[key] = (
            now + SCAN_REJECTION_LOG_INTERVAL_SECONDS
        )
        LOG.debug(
            "Rejected advertisement address=%s type=%s name=%r RSSI=%s "
            "reason=%s%s",
            address,
            getattr(event, "address_type", "?"),
            name,
            getattr(event, "rssi", "?"),
            reason,
            f" ({detail})" if detail else "",
        )

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
            "[BGM220] connected; Bluetooth stack booted: %d.%d.%d build %d",
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
            self._debug_rejected_advertisement(
                repr(getattr(event, "address", "?")),
                "invalid-address",
                event,
            )
            return

        name = extract_advertised_name(event.data) or ""
        if self.target_address is not None and address != self.target_address:
            self._debug_rejected_advertisement(
                address,
                "address-mismatch",
                event,
                name,
                f"expected={self.target_address}",
            )
            return
        if self.target_address is None and not name:
            self._debug_rejected_advertisement(address, "no-name", event)
            return
        if self.target_address is None and not name.startswith(self.args.name_prefix):
            self._debug_rejected_advertisement(
                address,
                "wrong-prefix",
                event,
                name,
                f"expected={self.args.name_prefix!r}",
            )
            return
        if address in self.connection_by_address:
            self._debug_rejected_advertisement(
                address, "already-connected", event, name
            )
            return
        retry_remaining = self.retry_after_by_address.get(address, 0.0) - time.monotonic()
        if retry_remaining > 0.0:
            self._debug_rejected_advertisement(
                address,
                "retry-after",
                event,
                name,
                f"remaining={retry_remaining:.1f}s",
            )
            return

        LOG.debug(
            "Accepted advertisement candidate address=%s type=%d name=%r RSSI=%d",
            address,
            event.address_type,
            name,
            event.rssi,
        )

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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost("connection.open", exc)

    def on_connection_opened(self, event: Any) -> None:
        address = normalize_address(event.address)
        pending = self.pending_node
        self.pending_node = None
        if not self._has_connection_capacity():
            LOG.warning("Connection capacity exceeded; closing handle=%d", event.connection)
            try:
                self.lib.bt.connection.close(event.connection)
            except bgapi.bglib.CommandFailedError as exc:
                LOG.warning(
                    "Cannot close excess connection handle=%d: 0x%04x",
                    event.connection,
                    exc.errorcode,
                )
            except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
                self._raise_transport_lost("connection.close", exc)
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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost("gatt.discover_primary_services", exc)

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
        elif bytes(event.uuid) == self.target_time_uuid:
            state.time_characteristic = event.characteristic
            state.time_characteristic_uuid = uuid_text
            state.time_characteristic_properties = properties
            if not properties & GATT_PROPERTY_WRITE:
                LOG.warning(
                    "[%s] Time characteristic does not advertise Write; "
                    "time synchronization will be skipped",
                    state.address,
                )

    def on_gatt_procedure_completed(self, event: Any) -> None:
        state = self._state_for_event(event)
        if state is None:
            return
        if event.result != 0 and state.phase.startswith("sync_time"):
            LOG.warning(
                "[%s] Optional time synchronization failed phase=%s result=0x%04x; "
                "notifications remain active",
                state.address,
                state.phase,
                event.result,
            )
            self._finish_time_sync(state, succeeded=False)
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
            self._finish_notification_setup(state)
        elif state.phase.startswith("sync_time"):
            self._finish_time_sync(state, succeeded=True)

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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost("gatt.discover_characteristics", exc)

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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost(
                "gatt.set_characteristic_notification(cough)", exc
            )

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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost(
                "gatt.set_characteristic_notification(environment)", exc
            )

    def _finish_notification_setup(self, state: ConnectionState) -> None:
        LOG.info(
            "[%s] Notifications enabled: cough=%s environment=%s",
            state.address,
            state.cough_characteristic,
            state.environment_characteristic,
        )
        if self._start_time_sync(state, "connect"):
            return
        self._mark_running(state)

    def _start_time_sync(
        self,
        state: ConnectionState,
        reason: str,
        epoch: int | None = None,
    ) -> bool:
        """Start one per-node GATT time write without waiting for completion."""
        if state.time_characteristic is None:
            return False
        if not state.time_characteristic_properties & GATT_PROPERTY_WRITE:
            return False

        sync_epoch = int(time.time()) if epoch is None else int(epoch)
        if not 0 < sync_epoch <= 0xFFFFFFFF:
            LOG.warning(
                "[%s] Unix epoch %r does not fit uint32; skipping time sync",
                state.address,
                sync_epoch,
            )
            return False

        state.phase = f"sync_time_{reason}"
        state.pending_time_sync_epoch = sync_epoch
        state.pending_time_sync_reason = reason
        self._set_phase_deadline(state)
        try:
            self.lib.bt.gatt.write_characteristic_value(
                state.handle,
                state.time_characteristic,
                struct.pack("<I", sync_epoch),
            )
        except bgapi.bglib.CommandFailedError as exc:
            LOG.warning(
                "[%s] Optional time synchronization command failed: 0x%04x; "
                "notifications remain active",
                state.address,
                exc.errorcode,
            )
            self._finish_time_sync(state, succeeded=False)
            return True
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost("gatt.write_characteristic_value", exc)

        LOG.info(
            "[%s] Writing Unix epoch=%d to Time characteristic (%s)",
            state.address,
            sync_epoch,
            reason,
        )
        return True

    def _finish_time_sync(
        self, state: ConnectionState, succeeded: bool
    ) -> None:
        reason = state.pending_time_sync_reason
        epoch = state.pending_time_sync_epoch
        if succeeded:
            state.last_time_sync_epoch = epoch
            state.last_time_sync_reason = reason
            LOG.info(
                "[%s] Time synchronized epoch=%s reason=%s",
                state.address,
                epoch,
                reason,
            )
        state.pending_time_sync_epoch = None
        state.pending_time_sync_reason = None
        self._mark_running(state)

    def _mark_running(self, state: ConnectionState) -> None:
        state.phase = "running"
        state.phase_deadline = 0.0
        if not state.status_reported:
            self._emit_status(state, "connected")
            state.status_reported = True
            state.last_status_heartbeat_at = time.monotonic()
        self.start_scan()

    def _check_status_heartbeats(self, now: float) -> None:
        """Best-effort proof of life for each fully configured BLE link."""
        if not self._ncp_transport_alive():
            return
        for state in list(self.connections.values()):
            if state.phase != "running" or not state.status_reported:
                continue
            if (
                now - state.last_status_heartbeat_at
                < DEVICE_STATUS_HEARTBEAT_SECONDS
            ):
                continue
            self._emit_status_heartbeat(state)
            state.last_status_heartbeat_at = now

    def _emit_status_heartbeat(self, state: ConnectionState) -> None:
        received_at = utc_now_iso()
        message = self._next_envelope(state, "status", received_at)
        message.update(
            {
                "status": "connected",
                "heartbeat": True,
                "event_ts": received_at,
            }
        )
        self._publish_best_effort(message)

    def _check_daily_time_sync(self, now_utc: datetime | None = None) -> None:
        """At each UTC date boundary, resync all eligible connected nodes."""
        current = now_utc or datetime.now(timezone.utc)
        current_date = current.astimezone(timezone.utc).date()
        if current_date == self.utc_sync_date:
            return

        self.utc_sync_date = current_date
        sync_epoch = int(current.timestamp())
        LOG.info("UTC date changed to %s; resynchronizing connected nodes", current_date)
        for state in list(self.connections.values()):
            if state.phase != "running":
                continue
            self._start_time_sync(state, "utc_midnight", epoch=sync_epoch)

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

    def _publish_best_effort(self, message: dict[str, Any]) -> None:
        print(json.dumps(message, ensure_ascii=False), flush=True)
        if self.backend.enabled and not self.backend.send_best_effort(message):
            LOG.debug("Best-effort status heartbeat skipped; it was not queued.")

    def _confirm_indication_if_needed(self, state: ConnectionState, opcode: int) -> None:
        if opcode != ATT_HANDLE_VALUE_INDICATION:
            return
        try:
            self.lib.bt.gatt.send_characteristic_confirmation(state.handle)
        except bgapi.bglib.CommandFailedError as exc:
            LOG.error("[%s] Indication confirmation failed: 0x%04x", state.address, exc.errorcode)
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost(
                "gatt.send_characteristic_confirmation", exc
            )

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
        except (bgapi.bglib.CommandError, OSError, ConnectorException) as exc:
            self._raise_transport_lost("connection.close", exc)
