#!/usr/bin/env python3
"""Entry point for the modular BreathSense BLE host."""

from __future__ import annotations

import logging

from ble_central import BleCentral
from config import parse_args


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    BleCentral(args).run()


if __name__ == "__main__":
    main()
