"""Command-line argument parsing and validation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from constants import (
    DEFAULT_COUGH_UUID,
    DEFAULT_ENVIRONMENT_UUID,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_NAME_PREFIX,
    DEFAULT_SERVICE_UUID,
    DEFAULT_TIME_UUID,
)
from utils import normalize_uuid


def build_argument_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description=(
            "Connect Raspberry Pi/BGM220 NCP to an EFR32xG26 peripheral, "
            "subscribe to cough and environment notifications, and forward "
            "them to a backend."
        )
    )

    parser.add_argument(
        "serial_port",
        nargs="?",
        default="auto",
        help=(
            "BGM220 serial port or 'auto' (default). Auto mode ranks current "
            "ports by USB metadata and verifies the NCP with BGAPI hello."
        ),
    )
    parser.add_argument(
        "--bgm220-serial-number",
        help=(
            "Optional exact USB serial number used to narrow auto-discovery "
            "when several Silicon Labs/J-Link boards are attached."
        ),
    )
    parser.add_argument(
        "--xapi",
        default=str(script_dir / "sl_bt.xapi"),
        help="Path to sl_bt.xapi matching the BGM220 NCP stack.",
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--no-flow-control",
        action="store_true",
        help="Disable serial RTS/CTS only when required.",
    )
    parser.add_argument(
        "--name-prefix",
        default=DEFAULT_NAME_PREFIX,
        help="Connect to the first advertising name with this prefix.",
    )
    parser.add_argument(
        "--address",
        help="Optional exact xG26 address; overrides --name-prefix.",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=DEFAULT_MAX_CONNECTIONS,
        help=(
            "Maximum simultaneous xG26 connections when matching by name "
            "prefix (default: %(default)s). The BGM220 NCP configuration "
            "must support at least this many links."
        ),
    )
    parser.add_argument(
        "--service-uuid",
        default=DEFAULT_SERVICE_UUID,
        type=normalize_uuid,
        help="128-bit UUID of the BreathSense service.",
    )
    parser.add_argument(
        "--cough-uuid",
        default=DEFAULT_COUGH_UUID,
        type=normalize_uuid,
        help="UUID of the cough-event characteristic.",
    )
    parser.add_argument(
        "--environment-uuid",
        default=DEFAULT_ENVIRONMENT_UUID,
        type=normalize_uuid,
        help="UUID of the environment-data characteristic.",
    )
    parser.add_argument(
        "--time-uuid",
        default=DEFAULT_TIME_UUID,
        type=normalize_uuid,
        help=(
            "Optional writable uint32 Unix-time characteristic. Firmware "
            "without this characteristic remains supported."
        ),
    )
    parser.add_argument(
        "--backend-socket",
        help="Optional Unix stream socket exposed by the backend.",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=80,
        help="Scan interval in 0.625 ms units (80 = 50 ms).",
    )
    parser.add_argument(
        "--scan-window",
        type=int,
        default=40,
        help="Scan window in 0.625 ms units (40 = 25 ms).",
    )
    parser.add_argument(
        "-l",
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )

    return parser


def parse_args() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    args = build_argument_parser().parse_args()

    if args.scan_window > args.scan_interval:
        raise SystemExit(
            "--scan-window must be less than or equal to --scan-interval."
        )

    if not 1 <= args.max_connections <= 32:
        raise SystemExit("--max-connections must be between 1 and 32.")

    if args.address is not None and args.max_connections != 1:
        args.max_connections = 1

    if not os.path.isfile(args.xapi):
        raise SystemExit(
            f"sl_bt.xapi not found: {args.xapi}\n"
            "Copy the matching file or pass --xapi."
        )

    return args
