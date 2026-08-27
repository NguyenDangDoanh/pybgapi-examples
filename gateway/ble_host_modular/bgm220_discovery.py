"""Serial metadata discovery plus BGAPI handshake verification."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

import bgapi

LOG = logging.getLogger("breathsense.bgm220")


def _candidate_score(port: Any) -> int:
    score = 0
    if getattr(port, "vid", None) == 0x1366:
        score += 100
    manufacturer = str(getattr(port, "manufacturer", "") or "").lower()
    description = str(getattr(port, "description", "") or "").lower()
    if "segger" in manufacturer:
        score += 50
    if "silicon" in manufacturer:
        score += 50
    if "j-link" in description:
        score += 30
    if str(getattr(port, "device", "")).startswith("/dev/ttyACM"):
        score += 10
    return score


def find_bgm220_candidates(
    *,
    ports: Iterable[Any] | None = None,
    serial_number: str | None = None,
) -> list[str]:
    """Rank plausible NCP ports without trusting USB VID alone."""
    requested_serial = serial_number.strip().lower() if serial_number else None
    ranked: list[tuple[int, str]] = []
    if ports is None:
        # Keep the pure ranking/handshake code importable in test environments
        # that do not have pyserial; production auto-discovery requires it.
        from serial.tools import list_ports

        ports = list_ports.comports()
    for port in list(ports):
        device = str(getattr(port, "device", "") or "")
        if not device:
            continue
        actual_serial = str(getattr(port, "serial_number", "") or "").lower()
        if requested_serial and actual_serial != requested_serial:
            continue
        score = _candidate_score(port)
        if score <= 0:
            continue
        LOG.info(
            "[BGM220] candidate %s score=%d vid=%s pid=%s serial=%s "
            "manufacturer=%r description=%r",
            device,
            score,
            getattr(port, "vid", None),
            getattr(port, "pid", None),
            getattr(port, "serial_number", None),
            getattr(port, "manufacturer", None),
            getattr(port, "description", None),
        )
        ranked.append((score, device))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [device for _, device in ranked]


def bgapi_handshake(
    port: str,
    args: Any,
    *,
    connector_factory: Callable[..., Any] | None = None,
    library_factory: Callable[..., Any] | None = None,
) -> bool:
    """Open, hello, and close a disposable BGAPI session."""
    library = None
    try:
        connector_factory = connector_factory or bgapi.SerialConnector
        library_factory = library_factory or bgapi.BGLib
        connector = connector_factory(
            port,
            baudrate=args.baudrate,
            rtscts=not args.no_flow_control,
        )
        library = library_factory(connector, args.xapi)
        library.open()
        library.bt.system.hello()
        LOG.info("[BGM220] BGAPI handshake OK port=%s", port)
        return True
    except Exception as exc:
        LOG.warning("[BGM220] BGAPI handshake failed port=%s: %s", port, exc)
        return False
    finally:
        if library is not None:
            try:
                library.close()
            except Exception:
                LOG.debug("[BGM220] handshake cleanup failed", exc_info=True)


def discover_bgm220_port(
    args: Any,
    *,
    ports: Iterable[Any] | None = None,
    probe: Callable[[str, Any], bool] = bgapi_handshake,
) -> str | None:
    """Return the first metadata candidate that proves it is a BGAPI NCP."""
    LOG.info("[BGM220] scanning ports")
    configured = str(getattr(args, "serial_port", "auto") or "auto")
    candidates: list[str] = []
    if configured.lower() != "auto":
        candidates.append(configured)
    candidates.extend(
        find_bgm220_candidates(
            ports=ports,
            serial_number=getattr(args, "bgm220_serial_number", None),
        )
    )
    candidates = list(dict.fromkeys(candidates))
    for port in candidates:
        if probe(port, args):
            return port
    LOG.warning("[BGM220] no BGAPI NCP found; reconnecting scan will continue")
    return None
