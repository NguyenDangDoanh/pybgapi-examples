"""Parsers for BreathSense GATT notification payloads."""

from __future__ import annotations

import struct
from typing import Optional

COUGH_EVENT_STRUCT = struct.Struct("<BBIH")
ENVIRONMENT_STRUCT = struct.Struct("<hH")


def parse_cough_payload(payload: bytes) -> Optional[dict[str, int | str]]:
    """Parse the 8-byte cough-event payload."""
    if len(payload) != COUGH_EVENT_STRUCT.size:
        return None

    flags, cough_type, event_timestamp, event_counter = (
        COUGH_EVENT_STRUCT.unpack(payload)
    )

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


def parse_environment_payload(
    payload: bytes,
) -> Optional[dict[str, int | float]]:
    """Parse temperature_x100 and humidity_x100 from 4 bytes."""
    if len(payload) != ENVIRONMENT_STRUCT.size:
        return None

    temperature_x100, humidity_x100 = ENVIRONMENT_STRUCT.unpack(payload)

    return {
        "temperature_x100": temperature_x100,
        "temperature_c": temperature_x100 / 100.0,
        "humidity_x100": humidity_x100,
        "humidity_percent": humidity_x100 / 100.0,
    }
