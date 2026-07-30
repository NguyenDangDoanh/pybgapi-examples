#!/usr/bin/env python3
"""
BreathSense PAwR Gateway
Raspberry Pi (BGAPI host) + BGM220 (PAwR Advertiser)
Compatible with the current EFR32xG26 BreathSense PAwR contract.

Install location:
    ~/pybgapi-examples/example/breathsense_pawr_gateway/app.py

Pi/BGM220 -> EFR32 POLL, exactly 8 bytes:
    [0]     protocol version = 0x01
    [1]     opcode = 0x01
    [2]     target node = 0x01 or 0xFF
    [3]     reserved = 0x00
    [4:5]   gateway sequence, uint16 little-endian
    [6:7]   reserved = 0x0000

Pi/BGM220 -> EFR32 RESEND_REQUEST, exactly 8 bytes:
    [0]     protocol version = 0x01
    [1]     opcode = 0x02
    [2]     target node = 0x01
    [3]     reserved = 0x00
    [4:5]   missing sensor_sequence, uint16 little-endian
    [6:7]   reserved = 0x0000

EFR32 -> Pi/BGM220 telemetry, exactly 14 bytes:
    [0]     magic = 0xB5
    [1]     protocol version = 0x01
    [2]     node_id = 0x01
    [3]     flags
    [4:5]   sensor_sequence, uint16 little-endian
    [6:7]   temperature, int16 little-endian, unit 0.01 C
    [8:9]   humidity, uint16 little-endian, unit 0.01 %RH
    [10]    AI class
    [11]    AI confidence, 0..100
    [12:13] AI sequence, uint16 little-endian
"""

from __future__ import annotations

import os.path
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

# This file must remain under pybgapi-examples/example/... so common.util can
# be imported from the repository root.
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from common.util import (  # type: ignore
    ArgumentParser,
    BluetoothApp,
    CommandFailedError,
    get_connector,
)


# =============================================================================
# BreathSense application protocol
# =============================================================================

PROTOCOL_VERSION = 0x01

OP_POLL = 0x01
OP_RESEND_REQUEST = 0x02

BROADCAST_NODE = 0xFF

COMMAND_PACKET_SIZE = 8
TELEMETRY_MAGIC = 0xB5
TELEMETRY_PACKET_SIZE = 14
TELEMETRY_STRUCT = struct.Struct("<BBBBHhHBBH")


# =============================================================================
# PAwR configuration required by the current EFR32 firmware
# =============================================================================

# Periodic advertising interval uses 1.25 ms units.
PAWR_INTERVAL_MIN = 800          # 1000.00 ms
PAWR_INTERVAL_MAX = 801          # 1001.25 ms

# Auto-start extended advertising. No ordinary periodic advertising API is used.
PAWR_FLAGS = 0x02

PAWR_NUM_SUBEVENTS = 1
PAWR_SUBEVENT_INTERVAL = 80      # 100 ms, units of 1.25 ms
PAWR_RESPONSE_SLOT_DELAY = 40    # 50 ms, units of 1.25 ms
PAWR_RESPONSE_SLOT_SPACING = 80  # 10 ms, units of 0.125 ms
PAWR_RESPONSE_SLOTS = 1

PAWR_SUBEVENT_INDEX = 0
PAWR_RESPONSE_SLOT_START = 0
PAWR_ACTIVE_RESPONSE_SLOTS = 1

# The current EFR32 keeps 16 immutable telemetry payloads, including the newest.
# A maximum forward gap of 15 therefore remains potentially recoverable.
MAX_AUTO_RESEND_GAP = 15

# First integration uses LE 1M PHY. Silicon Labs PHY enum value 1 is LE 1M.
LE_PHY_1M = 1


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class TxRecord:
    """Downlink associated with the next response-slot report."""

    kind: str
    subevent: int
    node_id: int
    gateway_sequence: Optional[int] = None
    sensor_sequence: Optional[int] = None
    resend_attempt: int = 0
    queued_at: float = 0.0


@dataclass
class ResendItem:
    node_id: int
    sensor_sequence: int
    attempts: int = 0
    inflight: bool = False


@dataclass
class NodeState:
    last_sensor_sequence: Optional[int] = None
    telemetry_count: int = 0
    duplicate_count: int = 0
    recovered_count: int = 0
    last_seen_monotonic: float = 0.0


@dataclass(frozen=True)
class Telemetry:
    magic: int
    version: int
    node_id: int
    flags: int
    sensor_sequence: int
    temperature_centi: int
    humidity_centi: int
    ai_class: int
    ai_confidence: int
    ai_sequence: int

    @property
    def temperature_c(self) -> float:
        return self.temperature_centi / 100.0

    @property
    def humidity_rh(self) -> float:
        return self.humidity_centi / 100.0

    @property
    def sensor_valid(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def ai_valid(self) -> bool:
        return bool(self.flags & 0x02)


# =============================================================================
# Protocol encoding and decoding
# =============================================================================

def build_poll(target_node: int, gateway_sequence: int) -> bytes:
    """Build the exact 8-byte POLL required by BGM220_Pi_contract.md."""
    return struct.pack(
        "<BBBBH2x",
        PROTOCOL_VERSION,
        OP_POLL,
        target_node & 0xFF,
        0x00,
        gateway_sequence & 0xFFFF,
    )


def build_resend_request(target_node: int, sensor_sequence: int) -> bytes:
    """Build the exact 8-byte RESEND_REQUEST required by the EFR32 firmware."""
    return struct.pack(
        "<BBBBH2x",
        PROTOCOL_VERSION,
        OP_RESEND_REQUEST,
        target_node & 0xFF,
        0x00,
        sensor_sequence & 0xFFFF,
    )


def parse_telemetry(data: bytes) -> Telemetry:
    if len(data) != TELEMETRY_PACKET_SIZE:
        raise ValueError(
            f"telemetry length must be {TELEMETRY_PACKET_SIZE}, got {len(data)}"
        )

    telemetry = Telemetry(*TELEMETRY_STRUCT.unpack(data))

    if telemetry.magic != TELEMETRY_MAGIC:
        raise ValueError(f"bad telemetry magic 0x{telemetry.magic:02X}")

    if telemetry.version != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported telemetry protocol version {telemetry.version}"
        )

    if telemetry.ai_confidence > 100:
        raise ValueError(
            f"AI confidence outside 0..100: {telemetry.ai_confidence}"
        )

    return telemetry


def protocol_self_test() -> None:
    """Fail before opening BGM220 if a wire-format change was introduced."""
    assert build_poll(1, 0x1234).hex() == "0101010034120000"
    assert build_resend_request(1, 0x1234).hex() == "0102010034120000"

    sample = bytes.fromhex(
        "B5 01 01 03 "
        "2A 00 "       # sensor_sequence = 42
        "73 0A "       # 2675 -> 26.75 C
        "ED 17 "       # 6125 -> 61.25 %RH
        "00 5C "       # AI class 0, confidence 92
        "07 00"        # AI sequence 7
    )

    telemetry = parse_telemetry(sample)

    assert telemetry.node_id == 1
    assert telemetry.sensor_sequence == 42
    assert telemetry.temperature_centi == 2675
    assert telemetry.humidity_centi == 6125
    assert telemetry.ai_class == 0
    assert telemetry.ai_confidence == 92
    assert telemetry.ai_sequence == 7


# =============================================================================
# Gateway application
# =============================================================================

class App(BluetoothApp):
    """BGM220 PAwR Advertiser and BreathSense packet gateway."""

    def __init__(
        self,
        connector,
        *,
        target_node: int = 1,
        active_slots: int = PAWR_ACTIVE_RESPONSE_SLOTS,
        resend_retries: int = 3,
    ):
        super().__init__(connector)

        if not 0 <= target_node <= 0xFF:
            raise ValueError("target_node must fit in uint8")

        if active_slots != 1:
            raise ValueError(
                "Current BreathSense MVP requires exactly one active slot. "
                "Use --active-slots 1."
            )

        if resend_retries < 1:
            raise ValueError("resend_retries must be at least 1")

        self.target_node = target_node
        self.active_slots = active_slots
        self.resend_retries = resend_retries

        self.advertising_set: Optional[int] = None
        self.gateway_sequence = 0

        # One response report is expected for each marked response slot.
        # With one subevent and one slot, FIFO order maps reports to downlinks.
        self.tx_fifo: Deque[TxRecord] = deque()

        self.pending_resends: Deque[ResendItem] = deque()
        self.pending_resend_keys: set[tuple[int, int]] = set()

        self.nodes: Dict[int, NodeState] = {}

        self.last_response_report_counter: Optional[int] = None

        self.no_response_count = 0
        self.complete_response_count = 0
        self.bad_response_count = 0

    # -------------------------------------------------------------------------
    # Small helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _sl_status(response) -> int:
        """Return the sl_status_t/result field from a BGAPI command response."""
        return int(getattr(response, "result", 0))

    @staticmethod
    def _payload_hex(data: bytes) -> str:
        return data.hex(" ").upper() if data else "-"

    # -------------------------------------------------------------------------
    # BGM220 startup
    # -------------------------------------------------------------------------

    def bt_evt_system_boot(self, evt):
        self.tx_fifo.clear()
        self.pending_resends.clear()
        self.pending_resend_keys.clear()
        self.nodes.clear()

        self.gateway_sequence = 0
        self.last_response_report_counter = None

        self.no_response_count = 0
        self.complete_response_count = 0
        self.bad_response_count = 0

        # API 1: create advertising set.
        try:
            create_response = self.lib.bt.advertiser.create_set()
        except CommandFailedError as err:
            self.log.error(
                "API bt.advertiser.create_set() FAILED: %s",
                err,
            )
            raise

        self.advertising_set = create_response.handle

        self.log.info(
            "API bt.advertiser.create_set() "
            "params={} -> sl_status_t=0x%08X handle=%d",
            self._sl_status(create_response),
            self.advertising_set,
        )

        # API 2: explicitly request LE 1M primary and secondary advertising PHY.
        # Continue with stack defaults only if this host API does not expose the
        # command; earlier EFR32 scans already observed this advertiser on 1M.
        try:
            phy_response = self.lib.bt.extended_advertiser.set_phy(
                self.advertising_set,
                LE_PHY_1M,
                LE_PHY_1M,
            )

            self.log.info(
                "API bt.extended_advertiser.set_phy("
                "handle=%d, primary_phy=%d, secondary_phy=%d) "
                "-> sl_status_t=0x%08X",
                self.advertising_set,
                LE_PHY_1M,
                LE_PHY_1M,
                self._sl_status(phy_response),
            )
        except AttributeError:
            self.log.warning(
                "API bt.extended_advertiser.set_phy is unavailable in this host API; "
                "continuing with the BGM220 default advertising PHY"
            )
        except CommandFailedError as err:
            self.log.warning(
                "API bt.extended_advertiser.set_phy(handle=%d, primary=1M, secondary=1M) "
                "FAILED: %s; continuing with stack default",
                self.advertising_set,
                err,
            )

        # API 3: start PAwR advertiser. This is not ordinary periodic advertising.
        try:
            start_response = self.lib.bt.pawr_advertiser.start(
                self.advertising_set,
                PAWR_INTERVAL_MIN,
                PAWR_INTERVAL_MAX,
                PAWR_FLAGS,
                PAWR_NUM_SUBEVENTS,
                PAWR_SUBEVENT_INTERVAL,
                PAWR_RESPONSE_SLOT_DELAY,
                PAWR_RESPONSE_SLOT_SPACING,
                PAWR_RESPONSE_SLOTS,
            )
        except CommandFailedError as err:
            self.log.error(
                "API bt.pawr_advertiser.start("
                "set=%d, interval_min=%d, interval_max=%d, flags=0x%X, "
                "num_subevents=%d, subevent_interval=%d, "
                "response_slot_delay=%d, response_slot_spacing=%d, "
                "response_slots=%d) FAILED: %s",
                self.advertising_set,
                PAWR_INTERVAL_MIN,
                PAWR_INTERVAL_MAX,
                PAWR_FLAGS,
                PAWR_NUM_SUBEVENTS,
                PAWR_SUBEVENT_INTERVAL,
                PAWR_RESPONSE_SLOT_DELAY,
                PAWR_RESPONSE_SLOT_SPACING,
                PAWR_RESPONSE_SLOTS,
                err,
            )
            raise

        self.log.info(
            "API bt.pawr_advertiser.start("
            "set=%d, interval_min=%d, interval_max=%d, flags=0x%X, "
            "num_subevents=%d, subevent_interval=%d, "
            "response_slot_delay=%d, response_slot_spacing=%d, "
            "response_slots=%d) -> sl_status_t=0x%08X",
            self.advertising_set,
            PAWR_INTERVAL_MIN,
            PAWR_INTERVAL_MAX,
            PAWR_FLAGS,
            PAWR_NUM_SUBEVENTS,
            PAWR_SUBEVENT_INTERVAL,
            PAWR_RESPONSE_SLOT_DELAY,
            PAWR_RESPONSE_SLOT_SPACING,
            PAWR_RESPONSE_SLOTS,
            self._sl_status(start_response),
        )

        self.log.info(
            "PAwR READY: interval=1000.00..1001.25ms "
            "subevent=0 response_slot=0 target_node=%d"
            ,
            self.target_node,
        )

        self.log.info(
            "Extended advertising is auto-started by PAWR_FLAGS=0x%X; "
            "ordinary periodic advertising API is not used",
            PAWR_FLAGS,
        )

    # -------------------------------------------------------------------------
    # PAwR downlink scheduling
    # -------------------------------------------------------------------------

    def bt_evt_pawr_advertiser_subevent_data_request(self, evt):
        self.log.info(
            "EVENT pawr_advertiser_subevent_data_request: "
            "advertising_set=%d subevent_start=%d subevent_data_count=%d",
            evt.advertising_set,
            evt.subevent_start,
            evt.subevent_data_count,
        )

        if self.advertising_set is None:
            self.log.warning(
                "Ignoring subevent-data request before PAwR initialization"
            )
            return

        for offset in range(evt.subevent_data_count):
            subevent = evt.subevent_start + offset

            if subevent >= PAWR_NUM_SUBEVENTS:
                self.log.warning(
                    "Ignoring invalid requested subevent=%d",
                    subevent,
                )
                continue

            self._queue_subevent_data(subevent)

    def _queue_subevent_data(self, subevent: int) -> None:
        payload, record = self._build_next_downlink(subevent)

        try:
            response = self.lib.bt.pawr_advertiser.set_subevent_data(
                self.advertising_set,
                subevent,
                PAWR_RESPONSE_SLOT_START,
                self.active_slots,
                payload,
            )
        except CommandFailedError as err:
            self.log.error(
                "API bt.pawr_advertiser.set_subevent_data("
                "set=%d, subevent=%d, response_slot_start=%d, "
                "response_slot_count=%d, data_len=%d, payload=%s) "
                "FAILED: %s",
                self.advertising_set,
                subevent,
                PAWR_RESPONSE_SLOT_START,
                self.active_slots,
                len(payload),
                self._payload_hex(payload),
                err,
            )
            return

        record.queued_at = time.monotonic()

        if record.kind == "RESEND_REQUEST":
            item = self._find_resend_item(
                record.node_id,
                record.sensor_sequence,
            )

            if item is None:
                raise RuntimeError(
                    "RESEND_REQUEST item disappeared before queueing"
                )

            item.attempts += 1
            item.inflight = True
            record.resend_attempt = item.attempts

        self.tx_fifo.append(record)

        self.log.info(
            "API bt.pawr_advertiser.set_subevent_data("
            "set=%d, subevent=%d, response_slot_start=%d, "
            "response_slot_count=%d, data_len=%d, payload=%s) "
            "-> sl_status_t=0x%08X",
            self.advertising_set,
            subevent,
            PAWR_RESPONSE_SLOT_START,
            self.active_slots,
            len(payload),
            self._payload_hex(payload),
            self._sl_status(response),
        )

        if record.kind == "POLL":
            self.log.info(
                "TX POLL node=%d subevent=%d slot=0 gateway_seq=%d",
                record.node_id,
                subevent,
                record.gateway_sequence,
            )
        else:
            self.log.info(
                "TX RESEND_REQUEST node=%d subevent=%d slot=0 "
                "sensor_seq=%d retry=%d/%d",
                record.node_id,
                subevent,
                record.sensor_sequence,
                record.resend_attempt,
                self.resend_retries,
            )

    def _build_next_downlink(
        self,
        subevent: int,
    ) -> tuple[bytes, TxRecord]:
        # The stack may request data for a future event before the previous
        # response report arrives. Do not queue the same resend twice at once.
        resend_item = next(
            (
                item
                for item in self.pending_resends
                if not item.inflight
                and item.attempts < self.resend_retries
            ),
            None,
        )

        if resend_item is not None:
            return (
                build_resend_request(
                    resend_item.node_id,
                    resend_item.sensor_sequence,
                ),
                TxRecord(
                    kind="RESEND_REQUEST",
                    subevent=subevent,
                    node_id=resend_item.node_id,
                    sensor_sequence=resend_item.sensor_sequence,
                ),
            )

        sequence = self.gateway_sequence
        self.gateway_sequence = (self.gateway_sequence + 1) & 0xFFFF

        return (
            build_poll(self.target_node, sequence),
            TxRecord(
                kind="POLL",
                subevent=subevent,
                node_id=self.target_node,
                gateway_sequence=sequence,
            ),
        )

    # -------------------------------------------------------------------------
    # PAwR response reports
    # -------------------------------------------------------------------------

    def bt_evt_pawr_advertiser_response_report(self, evt):
        self._check_response_report_counter(evt.counter)

        tx_record = self.tx_fifo.popleft() if self.tx_fifo else None
        data = bytes(evt.data)

        # Full response-report log required by the EFR32 integration contract.
        self.log.info(
            "RX_REPORT counter=%d advertising_set=%d subevent=%d slot=%d "
            "data_status=%d len=%d payload=%s tx_power=%d rssi=%d cte_type=%d",
            evt.counter,
            evt.advertising_set,
            evt.subevent,
            evt.response_slot,
            evt.data_status,
            len(data),
            self._payload_hex(data),
            evt.tx_power,
            evt.rssi,
            evt.cte_type,
        )

        if evt.subevent != PAWR_SUBEVENT_INDEX:
            self.bad_response_count += 1
            self.log.warning(
                "Unexpected response subevent=%d; expected=%d",
                evt.subevent,
                PAWR_SUBEVENT_INDEX,
            )

        if evt.response_slot != PAWR_RESPONSE_SLOT_START:
            self.bad_response_count += 1
            self.log.warning(
                "Unexpected response slot=%d; expected=%d",
                evt.response_slot,
                PAWR_RESPONSE_SLOT_START,
            )

        # 0xFF means no packet was received in the marked response slot.
        if evt.data_status == 0xFF:
            self.no_response_count += 1

            if tx_record is None:
                self.log.warning(
                    "NO_RESPONSE subevent=%d slot=%d tx=unknown total=%d",
                    evt.subevent,
                    evt.response_slot,
                    self.no_response_count,
                )
            elif tx_record.kind == "POLL":
                self.log.warning(
                    "NO_RESPONSE to POLL node=%d gateway_seq=%d total=%d",
                    tx_record.node_id,
                    tx_record.gateway_sequence,
                    self.no_response_count,
                )
            else:
                self.log.warning(
                    "NO_RESPONSE to RESEND_REQUEST node=%d sensor_seq=%d "
                    "attempt=%d/%d",
                    tx_record.node_id,
                    tx_record.sensor_sequence,
                    tx_record.resend_attempt,
                    self.resend_retries,
                )
                self._resend_attempt_failed(
                    tx_record,
                    "no_response",
                )

            return

        # Protocol v1 requires one complete 14-byte response.
        if evt.data_status != 0:
            self.bad_response_count += 1

            self.log.warning(
                "INCOMPLETE_RESPONSE status=%d len=%d payload=%s",
                evt.data_status,
                len(data),
                self._payload_hex(data),
            )

            if tx_record is not None and tx_record.kind == "RESEND_REQUEST":
                self._resend_attempt_failed(
                    tx_record,
                    "incomplete_response",
                )

            return

        self.complete_response_count += 1

        if len(data) != TELEMETRY_PACKET_SIZE:
            self.bad_response_count += 1

            if len(data) == COMMAND_PACKET_SIZE:
                self.log.error(
                    "LEGACY_8_BYTE_RESPONSE received: payload=%s. "
                    "Current EFR32 firmware must return telemetry length=14.",
                    self._payload_hex(data),
                )
            else:
                self.log.error(
                    "BAD_RESPONSE_LENGTH expected=14 received=%d payload=%s",
                    len(data),
                    self._payload_hex(data),
                )

            if tx_record is not None and tx_record.kind == "RESEND_REQUEST":
                self._resend_attempt_failed(
                    tx_record,
                    "wrong_response_length",
                )

            return

        self._handle_telemetry(data, evt, tx_record)

    def _handle_telemetry(
        self,
        data: bytes,
        evt,
        tx_record: Optional[TxRecord],
    ) -> None:
        try:
            telemetry = parse_telemetry(data)
        except ValueError as err:
            self.bad_response_count += 1
            self.log.error(
                "INVALID_TELEMETRY error=%s payload=%s",
                err,
                self._payload_hex(data),
            )

            if tx_record is not None and tx_record.kind == "RESEND_REQUEST":
                self._resend_attempt_failed(
                    tx_record,
                    "invalid_telemetry",
                )

            return

        if (
            self.target_node != BROADCAST_NODE
            and telemetry.node_id != self.target_node
        ):
            self.bad_response_count += 1
            self.log.error(
                "UNEXPECTED_NODE expected=%d received=%d payload=%s",
                self.target_node,
                telemetry.node_id,
                self._payload_hex(data),
            )

            if tx_record is not None and tx_record.kind == "RESEND_REQUEST":
                self._resend_attempt_failed(
                    tx_record,
                    "wrong_node",
                )

            return

        state = self.nodes.setdefault(
            telemetry.node_id,
            NodeState(),
        )

        state.telemetry_count += 1
        state.last_seen_monotonic = time.monotonic()

        recovered = self._mark_resend_recovered(
            telemetry.node_id,
            telemetry.sensor_sequence,
        )

        if recovered:
            state.recovered_count += 1

        self._track_sensor_sequence(telemetry, state)

        if tx_record is not None and tx_record.kind == "RESEND_REQUEST":
            if tx_record.sensor_sequence != telemetry.sensor_sequence:
                self.log.warning(
                    "RESEND_RESPONSE_MISMATCH requested=%d received=%d node=%d",
                    tx_record.sensor_sequence,
                    telemetry.sensor_sequence,
                    telemetry.node_id,
                )

                self._resend_attempt_failed(
                    tx_record,
                    "wrong_sensor_sequence",
                )

        self.log.info(
            "RX TELEMETRY node=%d sensor_seq=%d flags=0x%02X "
            "sensor_valid=%d ai_valid=%d "
            "T=%.2fC RH=%.2f%% AI_class=%d confidence=%d%% ai_seq=%d "
            "counter=%d subevent=%d slot=%d rssi=%d "
            "telemetry_count=%d duplicate_count=%d%s",
            telemetry.node_id,
            telemetry.sensor_sequence,
            telemetry.flags,
            1 if telemetry.sensor_valid else 0,
            1 if telemetry.ai_valid else 0,
            telemetry.temperature_c,
            telemetry.humidity_rh,
            telemetry.ai_class,
            telemetry.ai_confidence,
            telemetry.ai_sequence,
            evt.counter,
            evt.subevent,
            evt.response_slot,
            evt.rssi,
            state.telemetry_count,
            state.duplicate_count,
            " RESEND_RECOVERED" if recovered else "",
        )

    # -------------------------------------------------------------------------
    # Sequence and resend management
    # -------------------------------------------------------------------------

    def _track_sensor_sequence(
        self,
        telemetry: Telemetry,
        state: NodeState,
    ) -> None:
        current = telemetry.sensor_sequence
        previous = state.last_sensor_sequence

        if previous is None:
            state.last_sensor_sequence = current

            self.log.info(
                "SEQ_INIT node=%d sensor_seq=%d",
                telemetry.node_id,
                current,
            )
            return

        delta = (current - previous) & 0xFFFF

        if delta == 0:
            # Si7021 updates every 5 seconds while PAwR POLL is about 1 second.
            # Receiving the same immutable payload repeatedly is expected.
            state.duplicate_count += 1

            self.log.info(
                "DUPLICATE_VALID node=%d sensor_seq=%d duplicate_count=%d",
                telemetry.node_id,
                current,
                state.duplicate_count,
            )
            return

        if delta < 0x8000:
            # Forward movement, including uint16 wrap-around.
            if delta > 1:
                missing_count = delta - 1

                self.log.warning(
                    "SEQ_GAP node=%d previous=%d current=%d missing=%d",
                    telemetry.node_id,
                    previous,
                    current,
                    missing_count,
                )

                if missing_count <= MAX_AUTO_RESEND_GAP:
                    for step in range(1, delta):
                        missing_sequence = (previous + step) & 0xFFFF

                        self._enqueue_resend(
                            telemetry.node_id,
                            missing_sequence,
                        )
                else:
                    self.log.error(
                        "SEQ_GAP_TOO_LARGE node=%d missing=%d "
                        "history_capacity=16 auto_resend_limit=%d",
                        telemetry.node_id,
                        missing_count,
                        MAX_AUTO_RESEND_GAP,
                    )

            state.last_sensor_sequence = current
            return

        # Backward movement is normally an old duplicate or recovered resend.
        if (
            telemetry.node_id,
            current,
        ) not in self.pending_resend_keys:
            self.log.info(
                "OLD_OR_OUT_OF_ORDER node=%d sensor_seq=%d latest=%d",
                telemetry.node_id,
                current,
                previous,
            )

    def _enqueue_resend(
        self,
        node_id: int,
        sensor_sequence: int,
    ) -> None:
        key = (node_id, sensor_sequence)

        if key in self.pending_resend_keys:
            return

        self.pending_resend_keys.add(key)

        self.pending_resends.append(
            ResendItem(
                node_id=node_id,
                sensor_sequence=sensor_sequence,
            )
        )

        self.log.info(
            "RESEND_QUEUED node=%d sensor_seq=%d queue_depth=%d",
            node_id,
            sensor_sequence,
            len(self.pending_resends),
        )

    def _find_resend_item(
        self,
        node_id: int,
        sensor_sequence: Optional[int],
    ) -> Optional[ResendItem]:
        if sensor_sequence is None:
            return None

        for item in self.pending_resends:
            if (
                item.node_id == node_id
                and item.sensor_sequence == sensor_sequence
            ):
                return item

        return None

    def _mark_resend_recovered(
        self,
        node_id: int,
        sensor_sequence: int,
    ) -> bool:
        key = (node_id, sensor_sequence)

        if key not in self.pending_resend_keys:
            return False

        self.pending_resend_keys.discard(key)

        self.pending_resends = deque(
            item
            for item in self.pending_resends
            if (
                item.node_id,
                item.sensor_sequence,
            ) != key
        )

        self.log.info(
            "RESEND_RECOVERED node=%d sensor_seq=%d remaining=%d",
            node_id,
            sensor_sequence,
            len(self.pending_resends),
        )

        return True

    def _resend_attempt_failed(
        self,
        record: TxRecord,
        reason: str,
    ) -> None:
        item = self._find_resend_item(
            record.node_id,
            record.sensor_sequence,
        )

        if item is None:
            return

        item.inflight = False

        if item.attempts >= self.resend_retries:
            key = (item.node_id, item.sensor_sequence)
            self.pending_resend_keys.discard(key)

            self.pending_resends = deque(
                candidate
                for candidate in self.pending_resends
                if (
                    candidate.node_id,
                    candidate.sensor_sequence,
                ) != key
            )

            self.log.error(
                "PACKET_LOST_PERMANENT node=%d sensor_seq=%d "
                "retries=%d reason=%s",
                item.node_id,
                item.sensor_sequence,
                item.attempts,
                reason,
            )
            return

        self.log.warning(
            "RESEND_RETRY_PENDING node=%d sensor_seq=%d "
            "attempts=%d/%d reason=%s",
            item.node_id,
            item.sensor_sequence,
            item.attempts,
            self.resend_retries,
            reason,
        )

    # -------------------------------------------------------------------------
    # Controller diagnostics
    # -------------------------------------------------------------------------

    def bt_evt_pawr_advertiser_subevent_tx_failed(self, evt):
        tx_record = self.tx_fifo.popleft() if self.tx_fifo else None

        self.log.error(
            "EVENT pawr_advertiser_subevent_tx_failed: "
            "advertising_set=%d subevent=%d tx=%s",
            evt.advertising_set,
            evt.subevent,
            tx_record.kind if tx_record is not None else "unknown",
        )

        if (
            tx_record is not None
            and tx_record.kind == "RESEND_REQUEST"
        ):
            self._resend_attempt_failed(
                tx_record,
                "subevent_tx_failed",
            )

    def _check_response_report_counter(self, counter: int) -> None:
        # Silicon Labs response-report event counter is uint8 and wraps 255 -> 0.
        if self.last_response_report_counter is None:
            self.last_response_report_counter = counter
            return

        expected = (self.last_response_report_counter + 1) & 0xFF

        if counter != expected:
            dropped = (counter - expected) & 0xFF

            self.log.warning(
                "RESPONSE_REPORT_COUNTER_GAP expected=%d received=%d "
                "possibly_dropped=%d",
                expected,
                counter,
                dropped,
            )

        self.last_response_report_counter = counter


# =============================================================================
# Program entry point
# =============================================================================

def main() -> None:
    protocol_self_test()

    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--target-node",
        type=lambda value: int(value, 0),
        default=1,
        help="EFR32 node ID to POLL; use 0xFF only for broadcast testing",
    )

    parser.add_argument(
        "--active-slots",
        type=int,
        default=1,
        help="Current MVP requires exactly one active response slot",
    )

    parser.add_argument(
        "--resend-retries",
        type=int,
        default=3,
        help="Maximum RESEND_REQUEST attempts for one missing sensor_sequence",
    )

    args = parser.parse_args()
    connector = get_connector(args)

    app = App(
        connector,
        target_node=args.target_node,
        active_slots=args.active_slots,
        resend_retries=args.resend_retries,
    )

    app.run()


if __name__ == "__main__":
    main()
