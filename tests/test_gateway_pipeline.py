from __future__ import annotations

import os
import sqlite3
import socket
import struct
import sys
import threading
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.app.dao import Dao
from gateway.app.event_processor import EventProcessor
from gateway.app.fleet import Fleet

SCHEMA = ROOT / "gateway" / "app" / "schema.sql"


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"
        self.dao = Dao(str(self.db_path))
        self.dao.init_db(str(SCHEMA))
        self.processor = EventProcessor(self.dao, Fleet(self.dao))

    def tearDown(self) -> None:
        self.dao.close()
        self.tempdir.cleanup()

    @staticmethod
    def cough_message(device: str, message_id: str, counter: int) -> dict:
        return {
            "schema_version": 1,
            "message_id": message_id,
            "session_id": "host-session-a",
            "event": "cough_event",
            "received_at": "2026-07-30T02:00:00.123Z",
            "device": {
                "name": f"MyDevice_{device[-2:]}",
                "address": device,
                "address_type": 0,
                "connection_handle": 1,
            },
            "payload_hex": "0001000000000700",
            "parsed": {
                "flags": 0,
                "cough_type": 1,
                "cough_type_name": "dry",
                "event_timestamp": 0,
                "event_counter": counter,
            },
        }

    def test_two_nodes_with_same_counter_are_both_stored(self) -> None:
        first = self.cough_message("aa:bb:cc:dd:ee:01", "m-1", 7)
        second = self.cough_message("aa:bb:cc:dd:ee:02", "m-2", 7)
        self.processor.process(first)
        self.processor.process(second)

        rows = self.dao.get_recent_events(10)
        self.assertEqual(2, len(rows))
        self.assertEqual(
            {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"},
            {row["device_id"] for row in rows},
        )
        self.assertTrue(all(row["event_ts"] for row in rows))
        self.assertTrue(all(row["event_ts"] == row["received_ts"] for row in rows))

    def test_duplicate_retry_is_not_stored_twice(self) -> None:
        message = self.cough_message("aa:bb:cc:dd:ee:01", "m-retry", 9)
        self.processor.process(message)
        self.processor.process(message)
        self.assertEqual(1, len(self.dao.get_recent_events(10)))

    def test_counter_reset_does_not_report_uint16_sized_gap(self) -> None:
        self.processor.process(self.cough_message("aa:bb:cc:dd:ee:01", "m-10", 100))
        self.processor.process(self.cough_message("aa:bb:cc:dd:ee:01", "m-11", 1))
        self.assertEqual(2, len(self.dao.get_recent_events(10)))

    def test_reconnect_allows_same_counter_in_new_connection_sequence(self) -> None:
        device = "aa:bb:cc:dd:ee:01"
        self.processor.process(self.cough_message(device, "m-before-reconnect", 7))
        self.processor.process(
            {
                "event": "status",
                "message_id": "status-offline",
                "session_id": "host-session-a",
                "received_at": "2026-07-30T02:00:01.000Z",
                "device": {"address": device},
                "status": "disconnected",
            }
        )
        self.processor.process(
            {
                "event": "status",
                "message_id": "status-online",
                "session_id": "host-session-a",
                "received_at": "2026-07-30T02:00:02.000Z",
                "device": {"address": device},
                "status": "connected",
            }
        )
        self.processor.process(self.cough_message(device, "m-after-reconnect", 7))
        self.assertEqual(2, len(self.dao.get_recent_events(10)))

    def test_environment_is_stored_and_joined_into_fleet(self) -> None:
        self.processor.process(
            {
                "schema_version": 1,
                "message_id": "env-1",
                "session_id": "host-session-a",
                "event": "environment_data",
                "received_at": "2026-07-30T02:00:01.000Z",
                "device": {
                    "name": "MyDevice_01",
                    "address": "aa:bb:cc:dd:ee:01",
                    "address_type": 0,
                },
                "payload_hex": "c409c409",
                "parsed": {
                    "temperature_x100": 2500,
                    "temperature_c": 25.0,
                    "humidity_x100": 2500,
                    "humidity_percent": 25.0,
                },
            }
        )
        readings = self.dao.get_recent_environment(10)
        self.assertEqual(1, len(readings))
        devices = self.dao.get_devices()
        self.assertEqual(25.0, devices[0]["temperature_c"])
        self.assertEqual(25.0, devices[0]["humidity_percent"])
        self.assertEqual("online", devices[0]["status"])

    def test_out_of_range_environment_is_rejected(self) -> None:
        self.processor.process(
            {
                "event": "environment_data",
                "message_id": "env-invalid",
                "received_at": "2026-07-30T02:00:01.000Z",
                "device": {"address": "aa:bb:cc:dd:ee:01"},
                "parsed": {
                    "temperature_c": 25.0,
                    "humidity_percent": 140.0,
                    "temperature_x100": 2500,
                    "humidity_x100": 14000,
                },
            }
        )
        self.assertEqual([], self.dao.get_recent_environment(10))


class MigrationTest(unittest.TestCase):
    def test_old_database_is_migrated_without_deleting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "old.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE devices (
                    device_id TEXT PRIMARY KEY, client_id TEXT, assigned_at TEXT,
                    status TEXT, last_seen TEXT
                );
                CREATE TABLE cough_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL, client_id TEXT, cough_type TEXT,
                    event_ts TEXT, received_ts TEXT NOT NULL, event_counter INTEGER
                );
                INSERT INTO devices(device_id, status) VALUES ('node-old', 'online');
                INSERT INTO cough_events(
                    device_id, cough_type, event_ts, received_ts, event_counter
                ) VALUES ('node-old', 'dry', NULL, '2026-07-30T02:00:00Z', 1);
                """
            )
            conn.commit()
            conn.close()

            dao = Dao(str(db_path))
            dao.init_db(str(SCHEMA))
            try:
                row = dao.get_recent_events(1)[0]
                self.assertEqual(row["received_ts"], row["event_ts"])
                columns = {
                    item[1]
                    for item in dao._get_conn().execute("PRAGMA table_info(cough_events)")
                }
                self.assertIn("message_id", columns)
                self.assertIn("timestamp_source", columns)
                self.assertEqual([], dao.get_recent_environment(10))
            finally:
                dao.close()


class SocketConcurrencyTest(unittest.TestCase):
    def test_two_clients_can_deliver_without_blocking_each_other(self) -> None:
        from gateway.app.socket_server import SocketServer

        server = SocketServer()
        received: list[str] = []
        lock = threading.Lock()
        done = threading.Event()

        def callback(payload: dict) -> None:
            with lock:
                received.append(payload["device_id"])
                if len(received) == 2:
                    done.set()

        client_1, server_1 = socket.socketpair()
        client_2, server_2 = socket.socketpair()
        threads = [
            threading.Thread(
                target=server._handle_client,
                args=(server_1, "peer-1", callback),
                daemon=True,
            ),
            threading.Thread(
                target=server._handle_client,
                args=(server_2, "peer-2", callback),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        client_1.sendall(b'{"device_id":"node-1"}\n')
        client_2.sendall(b'{"device_id":"node-2"}\n')
        self.assertTrue(done.wait(1.0))
        client_1.close()
        client_2.close()
        self.assertEqual({"node-1", "node-2"}, set(received))


class BleRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The CI/container does not need pybgapi to exercise pure routing logic.
        fake_bgapi = types.ModuleType("bgapi")
        fake_bgapi.bglib = SimpleNamespace(
            CommandFailedError=type("CommandFailedError", (Exception,), {}),
            BGLibError=type("BGLibError", (Exception,), {}),
        )
        sys.modules.setdefault("bgapi", fake_bgapi)
        sys.path.insert(0, str(ROOT / "gateway" / "ble_host_modular"))

    def test_pending_connection_timeout_unblocks_future_scans(self) -> None:
        from ble_central import BleCentral
        from constants import CONNECT_TIMEOUT_SECONDS
        from models import PendingNode

        central = BleCentral.__new__(BleCentral)
        central.pending_node = PendingNode(
            address="aa:bb:cc:dd:ee:01",
            address_type=0,
            name="MyDevice_01",
            started_at=10.0,
        )
        central.retry_after_by_address = {}
        central.connections = {}
        central.scan_retry_after = 0.0
        central._check_timeouts(10.0 + CONNECT_TIMEOUT_SECONDS + 0.1)

        self.assertIsNone(central.pending_node)
        self.assertIn("aa:bb:cc:dd:ee:01", central.retry_after_by_address)
        self.assertGreater(central.scan_retry_after, 0.0)

    def test_gatt_timeout_disconnects_only_stalled_node(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        stalled = ConnectionState(
            handle=1,
            address="aa:bb:cc:dd:ee:01",
            address_type=0,
            name="MyDevice_01",
            phase="discover_characteristics",
            phase_deadline=5.0,
        )
        running = ConnectionState(
            handle=2,
            address="aa:bb:cc:dd:ee:02",
            address_type=0,
            name="MyDevice_02",
            phase="running",
            phase_deadline=0.0,
        )
        disconnected: list[int] = []
        central = BleCentral.__new__(BleCentral)
        central.pending_node = None
        central.connections = {1: stalled, 2: running}
        central.disconnect_connection = disconnected.append
        central._check_timeouts(6.0)

        self.assertEqual([1], disconnected)

    def test_same_characteristic_handle_on_two_connections_routes_by_connection(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        sent: list[dict] = []
        central = BleCentral.__new__(BleCentral)
        central.connections = {
            1: ConnectionState(
                handle=1,
                address="aa:bb:cc:dd:ee:01",
                address_type=0,
                name="MyDevice_01",
                cough_characteristic=20,
                cough_characteristic_uuid="cough",
                environment_characteristic=21,
                environment_characteristic_uuid="environment",
                phase="running",
            ),
            2: ConnectionState(
                handle=2,
                address="aa:bb:cc:dd:ee:02",
                address_type=0,
                name="MyDevice_02",
                cough_characteristic=20,
                cough_characteristic_uuid="cough",
                environment_characteristic=21,
                environment_characteristic_uuid="environment",
                phase="running",
            ),
        }
        central.session_id = "test-session"
        central.sequence = 0
        central.backend = SimpleNamespace(
            enabled=True,
            send=lambda message: sent.append(message) or True,
        )
        central.lib = SimpleNamespace(gatt=SimpleNamespace())

        for connection, counter in ((1, 11), (2, 12)):
            central.on_gatt_characteristic_value(
                SimpleNamespace(
                    connection=connection,
                    characteristic=20,
                    att_opcode=0x1B,
                    value=struct.pack("<BBIH", 0, 1, 0, counter),
                )
            )

        self.assertEqual(2, len(sent))
        self.assertEqual(
            ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            [message["device"]["address"] for message in sent],
        )
        self.assertEqual([11, 12], [message["parsed"]["event_counter"] for message in sent])
        self.assertTrue(all(message["event_ts"] for message in sent))


if __name__ == "__main__":
    unittest.main()
