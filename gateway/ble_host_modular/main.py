#!/usr/bin/env python3
"""Entry point for the modular BreathSense BLE host."""

from __future__ import annotations

import logging
import time

from ble_central import BleCentral, NcpTransportLost
from config import parse_args
from constants import NCP_RETRY_SECONDS


LOG = logging.getLogger("breathsense.supervisor")


def supervise(
    args,
    central_factory=BleCentral,
    sleep=time.sleep,
) -> None:
    """Run one BLE host at a time and recreate it after NCP transport loss."""
    while True:
        try:
            central_factory(args).run()
            break
        except NcpTransportLost as exc:
            LOG.warning(
                "%s; retrying BGM220 in %.1f seconds",
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

    supervise(args)


if __name__ == "__main__":
    main()
