"""Optional Unix-domain JSON Lines backend client."""

from __future__ import annotations

import json
import logging
import socket
import time
from collections import deque
from typing import Any, Optional

from constants import BACKEND_QUEUE_LIMIT, BACKEND_RETRY_SECONDS

LOG = logging.getLogger("breathsense.backend")


class JsonLineBackend:
    """Reconnectable Unix socket client with a bounded retry queue."""

    def __init__(
        self,
        socket_path: Optional[str],
        queue_limit: int = BACKEND_QUEUE_LIMIT,
    ) -> None:
        self.socket_path = socket_path
        self._socket: Optional[socket.socket] = None
        self._next_retry_at = 0.0
        self._queue: deque[bytes] = deque(maxlen=queue_limit)

    @property
    def enabled(self) -> bool:
        return bool(self.socket_path)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _connect(self) -> bool:
        if not self.socket_path:
            return False

        now = time.monotonic()
        if now < self._next_retry_at:
            return False

        self.close()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)

        try:
            client.connect(self.socket_path)
        except OSError as exc:
            client.close()
            self._next_retry_at = now + BACKEND_RETRY_SECONDS
            LOG.warning(
                "Backend socket unavailable (%s): %s",
                self.socket_path,
                exc,
            )
            return False

        client.settimeout(None)
        self._socket = client
        self._next_retry_at = 0.0
        LOG.info("Connected to backend socket: %s", self.socket_path)
        return True

    @staticmethod
    def _encode(message: dict[str, Any]) -> bytes:
        text = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")

    def _send_encoded(self, encoded: bytes) -> bool:
        if self._socket is None and not self._connect():
            return False

        try:
            assert self._socket is not None
            self._socket.sendall(encoded)
            return True
        except OSError as exc:
            LOG.warning("Backend send failed: %s", exc)
            self.close()
            return False

    def send(self, message: dict[str, Any]) -> bool:
        """Send a message or queue it temporarily when offline."""
        if not self.enabled:
            return False

        self.flush_pending()

        encoded = self._encode(message)
        if self._send_encoded(encoded):
            return True

        queue_was_full = len(self._queue) == self._queue.maxlen
        self._queue.append(encoded)

        if queue_was_full:
            LOG.error(
                "Backend queue full; oldest message dropped. queued=%d",
                len(self._queue),
            )
        else:
            LOG.warning(
                "Backend offline; message queued. queued=%d",
                len(self._queue),
            )
        return False

    def flush_pending(self) -> int:
        """Try to deliver queued messages in FIFO order."""
        if not self.enabled or not self._queue:
            return 0

        delivered = 0
        while self._queue:
            if not self._send_encoded(self._queue[0]):
                break
            self._queue.popleft()
            delivered += 1

        if delivered:
            LOG.info(
                "Delivered %d queued backend message(s); remaining=%d",
                delivered,
                len(self._queue),
            )
        return delivered
