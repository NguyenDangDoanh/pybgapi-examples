"""Ingest JSON lines from the BLE host process via a local Unix socket.

Runs in a daemon thread started by main.py.  Each complete JSON line is
passed to the on_event callback (EventProcessor.process).

See design/gateway_app.md.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from collections.abc import Callable


class SocketServer:
    """Listens for events from ble_host/socket_client with OS auto-detection."""

    def __init__(self, sock_path: str = "/tmp/cough_gw.sock", host: str = "127.0.0.1", port: int = 9000) -> None:
        self.sock_path = sock_path
        self.host = host
        self.port = port
        self.is_windows = sys.platform == "win32"

    def serve_forever(self, on_event: Callable[[dict], None]) -> None:
        """Starts the appropriate server based on the OS."""
        if self.is_windows:
            self._serve_tcp(on_event)
        else:
            self._serve_unix(on_event)

    def _serve_tcp(self, on_event: Callable[[dict], None]) -> None:
        """Setup TCP Socket for Windows environment."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((self.host, self.port))
            server_sock.listen()
            logging.info(f"SocketServer listening on TCP {self.host}:{self.port} (Windows mode)")
            self._accept_loop(server_sock, on_event)

    def _serve_unix(self, on_event: Callable[[dict], None]) -> None:
        """Setup Unix Domain Socket for Linux/Raspberry Pi environment."""
        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except OSError as e:
                logging.error(f"Failed to remove old socket file {self.sock_path}: {e}")
                return

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server_sock:
            server_sock.bind(self.sock_path)
            server_sock.listen()
            logging.info(f"SocketServer listening on UDS {self.sock_path} (Linux/Pi mode)")
            self._accept_loop(server_sock, on_event)

    def _accept_loop(self, server_sock: socket.socket, on_event: Callable[[dict], None]) -> None:
        """Accept connections and read JSON lines until interrupted."""
        while True:
            try:
                conn, _ = server_sock.accept()
                with conn:
                    with conn.makefile("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue

                            try:
                                payload = json.loads(line)
                                if isinstance(payload, dict):
                                    on_event(payload)
                                else:
                                    logging.warning("Skipped: JSON payload is not a dictionary.")
                            except json.JSONDecodeError as e:
                                logging.warning(f"Malformed JSON skipped: {e} | Data: {line}")
                            except Exception as e:
                                logging.error(f"Error processing event: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"Socket connection error: {e}")
