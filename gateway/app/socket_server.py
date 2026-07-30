"""Concurrent JSON-lines ingest from BLE host processes."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from collections.abc import Callable

LOG = logging.getLogger(__name__)
MAX_LINE_BYTES = 64 * 1024


class SocketServer:
    """Unix socket on Pi, TCP loopback on Windows, one thread per client."""

    def __init__(
        self,
        sock_path: str = "/tmp/cough_gw.sock",
        host: str = "127.0.0.1",
        port: int = 9000,
    ) -> None:
        self.sock_path = sock_path
        self.host = host
        self.port = port
        self.is_windows = sys.platform == "win32"
        self.ready = threading.Event()
        self.startup_error: Exception | None = None

    def serve_forever(self, on_event: Callable[[dict], None]) -> None:
        try:
            if self.is_windows:
                self._serve_tcp(on_event)
            else:
                self._serve_unix(on_event)
        except Exception as exc:
            self.startup_error = exc
            self.ready.set()
            LOG.exception("Socket server stopped")

    def _serve_tcp(self, on_event: Callable[[dict], None]) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((self.host, self.port))
            server_sock.listen()
            self.ready.set()
            LOG.info("SocketServer listening on TCP %s:%d", self.host, self.port)
            self._accept_loop(server_sock, on_event)

    def _serve_unix(self, on_event: Callable[[dict], None]) -> None:
        self._remove_stale_unix_socket()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server_sock:
            server_sock.bind(self.sock_path)
            os.chmod(self.sock_path, 0o660)
            server_sock.listen()
            self.ready.set()
            LOG.info("SocketServer listening on UDS %s", self.sock_path)
            try:
                self._accept_loop(server_sock, on_event)
            finally:
                try:
                    os.unlink(self.sock_path)
                except FileNotFoundError:
                    pass

    def _remove_stale_unix_socket(self) -> None:
        if not os.path.exists(self.sock_path):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(self.sock_path)
        except OSError:
            os.unlink(self.sock_path)
        else:
            raise RuntimeError(
                f"Socket {self.sock_path} is already served by another gateway"
            )
        finally:
            probe.close()

    def _accept_loop(
        self,
        server_sock: socket.socket,
        on_event: Callable[[dict], None],
    ) -> None:
        while True:
            conn, peer = server_sock.accept()
            thread = threading.Thread(
                target=self._handle_client,
                args=(conn, peer, on_event),
                daemon=True,
                name="gateway-ingest-client",
            )
            thread.start()

    def _handle_client(
        self,
        conn: socket.socket,
        peer: object,
        on_event: Callable[[dict], None],
    ) -> None:
        LOG.info("BLE ingest client connected: %s", peer or "local")
        try:
            with conn, conn.makefile("rb") as stream:
                while True:
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE_BYTES:
                        LOG.warning("Oversized JSON line skipped")
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(MAX_LINE_BYTES + 1)
                        continue
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped.decode("utf-8"))
                        if not isinstance(payload, dict):
                            LOG.warning("Skipped JSON payload that is not an object")
                            continue
                        on_event(payload)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        LOG.warning("Malformed JSON skipped: %s", exc)
                    except Exception:
                        LOG.exception("Error processing ingested event")
        except OSError as exc:
            LOG.warning("Socket client disconnected with error: %s", exc)
        finally:
            LOG.info("BLE ingest client disconnected: %s", peer or "local")
