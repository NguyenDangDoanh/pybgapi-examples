from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BLE_ROOT = ROOT / "gateway" / "ble_host_modular"
if str(BLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BLE_ROOT))
if "bgapi" not in sys.modules:
    class FakeCommandError(Exception):
        pass

    class FakeCommandFailedError(FakeCommandError):
        def __init__(self, errorcode: int = 1) -> None:
            super().__init__(errorcode)
            self.errorcode = errorcode

    fake_bgapi = types.ModuleType("bgapi")
    fake_connector = types.ModuleType("bgapi.connector")
    fake_connector.ConnectorException = type(
        "ConnectorException", (Exception,), {}
    )
    fake_bgapi.bglib = SimpleNamespace(
        CommandError=FakeCommandError,
        CommandFailedError=FakeCommandFailedError,
    )
    fake_bgapi.connector = fake_connector
    sys.modules["bgapi"] = fake_bgapi
    sys.modules["bgapi.connector"] = fake_connector


from bgm220_discovery import (  # noqa: E402
    bgapi_handshake,
    discover_bgm220_port,
    find_bgm220_candidates,
)
from gateway.app.telemetry_queue import TelemetryQueue  # noqa: E402
from gateway.app.upload_worker import UploadWorker  # noqa: E402
from gateway.app.dao import Dao  # noqa: E402
from gateway.app.event_processor import EventProcessor  # noqa: E402
from gateway.app.fleet import Fleet  # noqa: E402


class Bgm220DiscoveryTest(unittest.TestCase):
    @staticmethod
    def _port(device: str, **kwargs):
        defaults = {
            "vid": None,
            "pid": None,
            "manufacturer": None,
            "description": None,
            "serial_number": None,
        }
        defaults.update(kwargs)
        return SimpleNamespace(device=device, **defaults)

    def test_candidates_are_ranked_by_metadata_not_tty_number(self) -> None:
        ports = [
            self._port("/dev/ttyACM0", description="Other ACM"),
            self._port(
                "/dev/ttyACM2",
                vid=0x1366,
                manufacturer="SEGGER",
                description="J-Link OB - CDC",
                serial_number="NCP-1",
            ),
        ]
        self.assertEqual(
            ["/dev/ttyACM2", "/dev/ttyACM0"],
            find_bgm220_candidates(ports=ports),
        )

    def test_multiple_serial_devices_require_successful_bgapi_probe(self) -> None:
        ports = [
            self._port("/dev/ttyACM0", vid=0x1366),
            self._port("/dev/ttyACM1", vid=0x1366),
        ]
        attempts: list[str] = []

        def probe(port: str, _args) -> bool:
            attempts.append(port)
            return port == "/dev/ttyACM1"

        selected = discover_bgm220_port(
            SimpleNamespace(serial_port="auto", bgm220_serial_number=None),
            ports=ports,
            probe=probe,
        )
        self.assertEqual("/dev/ttyACM1", selected)
        self.assertEqual(["/dev/ttyACM0", "/dev/ttyACM1"], attempts)

    def test_handshake_always_closes_disposable_library(self) -> None:
        calls: list[object] = []

        class Library:
            def __init__(self, connector, xapi) -> None:
                calls.append((connector, xapi))
                self.bt = SimpleNamespace(
                    system=SimpleNamespace(hello=lambda: calls.append("hello"))
                )

            def open(self) -> None:
                calls.append("open")

            def close(self) -> None:
                calls.append("close")

        args = SimpleNamespace(
            baudrate=115200,
            no_flow_control=False,
            xapi="sl_bt.xapi",
        )
        result = bgapi_handshake(
            "/dev/ttyACM7",
            args,
            connector_factory=lambda *args, **kwargs: (args, kwargs),
            library_factory=Library,
        )
        self.assertTrue(result)
        self.assertIn("hello", calls)
        self.assertEqual("close", calls[-1])

    def test_supervisor_resolves_a_fresh_port_after_transport_loss(self) -> None:
        from ble_central import NcpTransportLost
        from main import supervise

        ports = iter(("/dev/ttyACM0", "/dev/ttyACM1"))
        used: list[str] = []

        class Central:
            def __init__(self, args) -> None:
                self.port = args.serial_port
                used.append(self.port)

            def run(self) -> None:
                if self.port == "/dev/ttyACM0":
                    raise NcpTransportLost("unplugged")

        supervise(
            SimpleNamespace(),
            central_factory=Central,
            sleep=lambda _seconds: None,
            port_resolver=lambda _args: next(ports),
        )
        self.assertEqual(["/dev/ttyACM0", "/dev/ttyACM1"], used)


class TelemetryQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "gateway.db")
        self.queue = TelemetryQueue(self.db_path)
        self.queue.init_db()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_pending_survives_new_queue_instance_and_keeps_timestamp(self) -> None:
        event_id, inserted = self.queue.enqueue(
            {"event": "cough_event", "message_id": "evt-1"},
            event_id="evt-1",
            event_ts="2026-08-20T10:00:00.000Z",
        )
        self.assertTrue(inserted)
        self.assertEqual("evt-1", event_id)

        reopened = TelemetryQueue(self.db_path)
        rows = reopened.pending()
        self.assertEqual(1, len(rows))
        self.assertEqual("2026-08-20T10:00:00.000Z", rows[0]["event_ts"])

    def test_duplicate_event_id_is_not_queued_twice(self) -> None:
        payload = {"event": "environment_data", "message_id": "evt-dedup"}
        self.assertTrue(self.queue.enqueue(payload)[1])
        self.assertFalse(self.queue.enqueue(payload)[1])
        self.assertEqual(1, self.queue.pending_count())

    def test_failed_upload_is_never_deleted_and_retry_count_persists(self) -> None:
        self.queue.enqueue({"message_id": "evt-fail"})
        row = self.queue.pending()[0]
        self.queue.mark_failed(row["id"], "offline")

        reopened = TelemetryQueue(self.db_path)
        pending = reopened.pending()
        self.assertEqual(1, len(pending))
        self.assertEqual(1, pending[0]["retry_count"])

    def test_receipt_is_idempotent(self) -> None:
        self.assertTrue(self.queue.record_receipt("evt-ack"))
        self.assertFalse(self.queue.record_receipt("evt-ack"))
        self.assertTrue(self.queue.receipt_exists("evt-ack"))

    def test_worker_retries_then_marks_only_matching_ack_sent(self) -> None:
        self.queue.enqueue(
            {"event": "cough_event", "message_id": "evt-upload"},
            event_id="evt-upload",
            event_ts="2026-08-20T10:00:00.000Z",
        )
        attempts: list[dict] = []

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"ack": True, "event_id": "evt-upload"}

        def post(_url, envelope, _timeout, _headers):
            attempts.append(envelope)
            if len(attempts) == 1:
                raise OSError("network offline")
            return Response()

        worker = UploadWorker(
            self.queue,
            "http://server.test/api/telemetry",
            initial_backoff=0.01,
            max_backoff=0.02,
            idle_seconds=0.01,
            disk_check_seconds=3600,
            post=post,
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while self.queue.pending_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop()

        self.assertEqual(0, self.queue.pending_count())
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(
            "2026-08-20T10:00:00.000Z", attempts[-1]["timestamp"]
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            sent, retry_count = conn.execute(
                "SELECT sent, retry_count FROM telemetry_outbox WHERE event_id = ?",
                ("evt-upload",),
            ).fetchone()
        self.assertEqual((1, 1), (sent, retry_count))


class DurableIngestTest(unittest.TestCase):
    def test_sensor_event_is_committed_to_domain_and_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = str(Path(tempdir) / "gateway.db")
            dao = Dao(db_path)
            dao.init_db(str(ROOT / "gateway" / "app" / "schema.sql"))
            queue = TelemetryQueue(db_path)
            queue.init_db()
            processor = EventProcessor(dao, Fleet(dao), queue)
            accepted = processor.process(
                {
                    "message_id": "evt-durable",
                    "session_id": "session-1",
                    "event": "cough_event",
                    "received_at": "2026-08-20T10:00:02.000Z",
                    "event_ts": "2026-08-20T10:00:00.000Z",
                    "device": {
                        "device_id": "aa:bb:cc:dd:ee:01",
                        "address": "aa:bb:cc:dd:ee:01",
                    },
                    "parsed": {
                        "cough_type_name": "dry",
                        "event_counter": 1,
                        "event_timestamp": 1787210400,
                    },
                }
            )
            self.assertTrue(accepted)
            self.assertEqual(1, len(dao.get_recent_events()))
            self.assertEqual("evt-durable", queue.pending()[0]["event_id"])
            dao.close()

    def test_invalid_environment_is_not_written_or_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = str(Path(tempdir) / "gateway.db")
            dao = Dao(db_path)
            dao.init_db(str(ROOT / "gateway" / "app" / "schema.sql"))
            queue = TelemetryQueue(db_path)
            queue.init_db()
            processor = EventProcessor(dao, Fleet(dao), queue)

            accepted = processor.process(
                {
                    "message_id": "evt-invalid-environment",
                    "session_id": "session-1",
                    "event": "environment_data",
                    "received_at": "2026-08-20T10:00:02.000Z",
                    "device": {"device_id": "aa:bb:cc:dd:ee:01"},
                    "parsed": {
                        "temperature_c": 24.5,
                        "humidity_percent": 120.0,
                    },
                }
            )

            self.assertFalse(accepted)
            self.assertEqual(0, queue.pending_count())
            with closing(sqlite3.connect(db_path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM environment_readings"
                ).fetchone()[0]
            self.assertEqual(0, count)
            dao.close()


if __name__ == "__main__":
    unittest.main()
