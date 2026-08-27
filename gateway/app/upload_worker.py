"""Background uploader for the durable telemetry outbox."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .telemetry_queue import TelemetryQueue

LOG = logging.getLogger("breathsense.upload")


class _HttpResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return json.loads(self._body.decode("utf-8"))


def _post_json(
    url: str,
    envelope: dict[str, Any],
    timeout: float,
    headers: dict[str, str],
) -> _HttpResponse:
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return _HttpResponse(int(response.status), response.read())


class UploadWorker:
    """Upload oldest-first, retry forever, and delete no telemetry."""

    def __init__(
        self,
        queue: TelemetryQueue,
        upload_url: str | None,
        *,
        batch_size: int = 100,
        request_timeout: float = 10.0,
        initial_backoff: float = 5.0,
        max_backoff: float = 300.0,
        idle_seconds: float = 2.0,
        disk_check_seconds: float = 60.0,
        disk_warn_percent: float = 80.0,
        disk_critical_percent: float = 90.0,
        post: Callable[[str, dict[str, Any], float, dict[str, str]], Any]
        | None = None,
    ) -> None:
        self.queue = queue
        self.upload_url = upload_url.strip() if upload_url else None
        self.batch_size = min(max(int(batch_size), 1), 1000)
        self.request_timeout = max(float(request_timeout), 0.1)
        self.initial_backoff = max(float(initial_backoff), 0.1)
        self.max_backoff = max(float(max_backoff), self.initial_backoff)
        self.idle_seconds = max(float(idle_seconds), 0.1)
        self.disk_check_seconds = max(float(disk_check_seconds), 1.0)
        self.disk_warn_percent = float(disk_warn_percent)
        self.disk_critical_percent = max(
            float(disk_critical_percent), self.disk_warn_percent
        )
        self.post = post or _post_json
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_disk_check = 0.0
        self._network_state: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="telemetry-upload-worker",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(timeout, 0.0))

    def _wait(self, seconds: float) -> bool:
        return self._stop.wait(max(seconds, 0.0))

    def _set_network_state(self, state: str, detail: str = "") -> None:
        if state == self._network_state:
            return
        self._network_state = state
        if state == "online":
            LOG.info("[NET] online")
        elif state == "offline":
            LOG.warning("[NET] offline%s", f": {detail}" if detail else "")

    def _check_disk(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_disk_check < self.disk_check_seconds:
            return
        self._last_disk_check = now
        db_path = Path(self.queue.db_path)
        usage = shutil.disk_usage(db_path.parent)
        used_percent = 100.0 * (usage.total - usage.free) / max(usage.total, 1)
        db_bytes = sum(
            path.stat().st_size
            for path in (
                db_path,
                Path(str(db_path) + "-wal"),
                Path(str(db_path) + "-shm"),
            )
            if path.exists()
        )
        pending = self.queue.pending_count()
        LOG.info("[DB] pending=%d", pending)
        message = (
            "[DISK] usage=%.1f%% free_bytes=%d database_bytes=%d pending=%d"
        )
        args = (used_percent, usage.free, db_bytes, pending)
        if used_percent >= self.disk_critical_percent:
            LOG.error(message + " critical", *args)
        elif used_percent >= self.disk_warn_percent:
            LOG.warning(message + " warning", *args)
        else:
            LOG.info(message, *args)

    @staticmethod
    def _acknowledged(response: Any, event_id: str) -> bool:
        if not 200 <= int(getattr(response, "status_code", 0)) < 300:
            return False
        try:
            body = response.json()
        except (TypeError, ValueError):
            return False
        if not isinstance(body, dict) or str(body.get("event_id")) != event_id:
            return False
        return body.get("ack") is True or str(body.get("status", "")).lower() in {
            "accepted",
            "duplicate",
            "ok",
            "success",
        }

    def _upload(self, row: dict[str, Any]) -> None:
        event_id = str(row["event_id"])
        envelope = {
            "event_id": event_id,
            "timestamp": row["event_ts"],
            "data": row["payload"],
        }
        response = self.post(
            self.upload_url,
            envelope,
            self.request_timeout,
            {"Idempotency-Key": event_id},
        )
        if not self._acknowledged(response, event_id):
            raise RuntimeError(
                f"server did not ACK event_id={event_id} "
                f"status={getattr(response, 'status_code', 'unknown')}"
            )

    def run(self) -> None:
        backoff = self.initial_backoff
        if not self.upload_url:
            LOG.info("[UPLOAD] disabled; telemetry remains durable until URL is configured")
        while not self._stop.is_set():
            try:
                self._check_disk()
            except Exception:
                LOG.exception("[DISK] monitoring failed")

            if not self.upload_url:
                self._wait(self.idle_seconds)
                continue

            rows = self.queue.pending(self.batch_size)
            if not rows:
                backoff = self.initial_backoff
                self._wait(self.idle_seconds)
                continue

            failed = False
            for row in rows:
                if self._stop.is_set():
                    return
                try:
                    self._upload(row)
                    self.queue.mark_sent(row["id"], row["event_id"])
                    self._set_network_state("online")
                    LOG.info(
                        "[DB] sent event_id=%s pending=%d",
                        row["event_id"],
                        self.queue.pending_count(),
                    )
                    LOG.info(
                        "[UPLOAD] upload success event_id=%s pending=%d",
                        row["event_id"],
                        self.queue.pending_count(),
                    )
                    backoff = self.initial_backoff
                except Exception as exc:
                    self.queue.mark_failed(row["id"], str(exc))
                    self._set_network_state("offline", str(exc))
                    LOG.warning(
                        "[UPLOAD] upload failed event_id=%s retrying_in=%.1fs: %s",
                        row["event_id"],
                        backoff,
                        exc,
                    )
                    failed = True
                    break

            if failed:
                self._wait(backoff)
                backoff = min(backoff * 2.0, self.max_backoff)
            else:
                # Yield between batches so receiver writes are never starved.
                self._wait(0.01)


def worker_from_environment(queue: TelemetryQueue) -> UploadWorker:
    """Build one worker without embedding deployment-specific server details."""
    return UploadWorker(
        queue,
        os.environ.get("GATEWAY_UPLOAD_URL"),
        batch_size=int(os.environ.get("GATEWAY_UPLOAD_BATCH_SIZE", "100")),
        request_timeout=float(
            os.environ.get("GATEWAY_UPLOAD_TIMEOUT_SECONDS", "10")
        ),
        initial_backoff=float(
            os.environ.get("GATEWAY_UPLOAD_BACKOFF_INITIAL_SECONDS", "5")
        ),
        max_backoff=float(
            os.environ.get("GATEWAY_UPLOAD_BACKOFF_MAX_SECONDS", "300")
        ),
        disk_check_seconds=float(
            os.environ.get("GATEWAY_DISK_CHECK_SECONDS", "60")
        ),
        disk_warn_percent=float(
            os.environ.get("GATEWAY_DISK_WARN_PERCENT", "80")
        ),
        disk_critical_percent=float(
            os.environ.get("GATEWAY_DISK_CRITICAL_PERCENT", "90")
        ),
    )
