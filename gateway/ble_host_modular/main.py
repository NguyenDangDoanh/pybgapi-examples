#!/usr/bin/env python3
"""Entry point for the modular BreathSense BLE host."""

from __future__ import annotations

import logging
import time
from argparse import Namespace

from bgm220_discovery import discover_bgm220_port
from ble_central import BleCentral, NcpTransportLost
from config import parse_args
from constants import NCP_RETRY_SECONDS


LOG = logging.getLogger("breathsense.supervisor")


def supervise(
    args,
    central_factory=BleCentral,
    sleep=time.sleep,
    port_resolver=discover_bgm220_port,
) -> None:
    """Run one BLE host at a time and recreate it after NCP transport loss."""
    attempt = 0
    while True:
        try:
            serial_port = port_resolver(args)
        except KeyboardInterrupt:
            LOG.info("Stopped by user.")
            break
        except Exception as exc:
            LOG.exception("[BGM220] serial discovery failed: %s", exc)
            serial_port = None
        if serial_port is None:
            try:
                sleep(NCP_RETRY_SECONDS)
            except KeyboardInterrupt:
                LOG.info("Stopped by user.")
                break
            continue

        attempt += 1
        session_args = Namespace(**vars(args))
        session_args.serial_port = serial_port
        LOG.info(
            "Starting BGM220 host session attempt=%d serial=%s",
            attempt,
            serial_port,
        )
        try:
            central_factory(session_args).run()
            break
        except NcpTransportLost as exc:
            LOG.warning(
                "[BGM220] disconnected: %s; reconnecting in %.1f seconds",
                exc,
                NCP_RETRY_SECONDS,
            )
            try:
                sleep(NCP_RETRY_SECONDS)
            except KeyboardInterrupt:
                LOG.info("Stopped by user.")
                break
        except KeyboardInterrupt:
            LOG.info("Stopped by user.")
            break


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # pyBGAPI logs every raw advertisement at DEBUG. Keep project-level
    # scanner diagnostics useful without flooding or slowing the event loop.
    logging.getLogger("bgapi").setLevel(logging.WARNING)

    supervise(args)


if __name__ == "__main__":
    main()
