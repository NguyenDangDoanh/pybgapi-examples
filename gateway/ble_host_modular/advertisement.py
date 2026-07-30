"""BLE advertising-data parsing."""

from __future__ import annotations

from typing import Optional

from constants import AD_TYPE_COMPLETE_NAME, AD_TYPE_SHORT_NAME


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
            return ad_value.rstrip(b"\x00").decode(
                "utf-8",
                errors="replace",
            )

        offset = field_end

    return None
