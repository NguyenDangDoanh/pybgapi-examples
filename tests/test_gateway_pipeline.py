from __future__ import annotations

import os
import random
import sqlite3
import socket
import struct
import sys
import threading
import tempfile
import types
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.app.dao import Dao
from gateway.app.analytics import Analytics
from gateway.app.event_processor import EventProcessor
from gateway.app.fleet import Fleet
from gateway.app.simulate_dashboard_data import (
    LEGACY_DEMO_CLIENT_IDS,
    LEGACY_DEMO_DEVICE_IDS,
    PROFILES,
    SIM_PREFIX,
    cleanup_simulated_data,
    insert_live_event,
    refresh_simulated_device_status,
    simulate_history,
)

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
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

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
        self.assertTrue(all(row["node_event_timestamp"] == 0 for row in rows))
        self.assertEqual({7}, {row["event_counter"] for row in rows})
        self.assertTrue(all(row["timestamp_source"] == "gateway_received" for row in rows))

    def test_duplicate_retry_is_not_stored_twice(self) -> None:
        message = self.cough_message("aa:bb:cc:dd:ee:01", "m-retry", 9)
        self.processor.process(message)
        self.processor.process(message)
        self.assertEqual(1, len(self.dao.get_recent_events(10)))

    def test_extended_timestamp_keeps_node_and_receive_audit_fields(self) -> None:
        message = self.cough_message("aa:bb:cc:dd:ee:01", "m-timed", 10)
        message["event_ts"] = "2026-07-30T00:00:00.000Z"
        message["parsed"].update(
            {
                "event_timestamp": 1785369600,
                "event_timestamp_iso": "2026-07-30T00:00:00.000Z",
                "timestamp_source": "node_unix_seconds",
            }
        )

        self.processor.process(message)

        row = self.dao.get_recent_events(1)[0]
        self.assertEqual("2026-07-30T00:00:00.000Z", row["event_ts"])
        self.assertEqual("2026-07-30T02:00:00.123Z", row["received_ts"])
        self.assertEqual(1785369600, row["node_event_timestamp"])
        self.assertEqual(10, row["event_counter"])
        self.assertEqual("node_unix_seconds", row["timestamp_source"])
        self.assertEqual(
            "2026-07-30T02:00:00.123Z",
            self.dao.get_device("aa:bb:cc:dd:ee:01")["last_seen"],
        )

    def test_status_heartbeat_refreshes_last_seen_and_disconnects_immediately(self) -> None:
        device = "aa:bb:cc:dd:ee:01"
        for message_id, received_at, status, heartbeat in (
            ("connected", "2026-07-30T02:00:00.000Z", "connected", False),
            ("heartbeat", "2026-07-30T02:00:10.000Z", "connected", True),
            ("disconnected", "2026-07-30T02:00:11.000Z", "disconnected", False),
        ):
            self.processor.process(
                {
                    "event": "status",
                    "message_id": message_id,
                    "received_at": received_at,
                    "device": {"address": device},
                    "status": status,
                    "heartbeat": heartbeat,
                }
            )
            row = self.dao.get_device(device)
            self.assertEqual(received_at, row["last_seen"])
            self.assertEqual(
                "offline" if status == "disconnected" else "online",
                row["status"],
            )

    def test_stale_expiration_is_per_device_and_preserves_last_seen(self) -> None:
        stale_seen = "2026-08-23T00:00:00.000Z"
        recent_seen = "2026-08-23T00:00:25.000Z"
        self.dao.upsert_device("stale", status="online", last_seen=stale_seen)
        self.dao.upsert_device("recent", status="online", last_seen=recent_seen)
        self.dao.upsert_device("unknown-time", status="online", last_seen=None)

        changed = self.dao.mark_stale_devices_offline(
            "2026-08-23T00:00:20.000Z"
        )

        self.assertEqual(2, changed)
        self.assertEqual("offline", self.dao.get_device("stale")["status"])
        self.assertEqual(stale_seen, self.dao.get_device("stale")["last_seen"])
        self.assertEqual("online", self.dao.get_device("recent")["status"])
        self.assertEqual("offline", self.dao.get_device("unknown-time")["status"])

    def test_device_status_freshness_expires_stale_online_status(self) -> None:
        from gateway.app.device_status import (
            DEVICE_STATUS_STALE_SECONDS,
            expire_stale_device_status,
        )

        now = datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)
        stale_seen = now - timedelta(seconds=DEVICE_STATUS_STALE_SECONDS + 5)
        recent_seen = now - timedelta(seconds=DEVICE_STATUS_STALE_SECONDS - 5)
        self.dao.upsert_device(
            "stale-api", status="online", last_seen=self._iso(stale_seen)
        )
        self.dao.upsert_device(
            "recent-api", status="online", last_seen=self._iso(recent_seen)
        )
        changed = expire_stale_device_status(self.dao, now=now)

        self.assertEqual(1, changed)
        devices = {item["device_id"]: item for item in self.dao.get_devices()}
        self.assertEqual("offline", devices["stale-api"]["status"])
        self.assertEqual(self._iso(stale_seen), devices["stale-api"]["last_seen"])
        self.assertEqual("online", devices["recent-api"]["status"])

    def test_cough_bout_metadata_is_persisted(self) -> None:
        message = self.cough_message("aa:bb:cc:dd:ee:01", "m-bout", 11)
        message["parsed"].update(
            {
                "flags": 0x35,
                "cough_type": 0,
                "cough_type_name": "unknown",
                "event_timestamp": 1785369600,
                "event_timestamp_iso": "2026-07-30T00:00:00.000Z",
                "timestamp_source": "node_unix_seconds",
                "timestamp_valid": True,
                "stage2_valid": False,
                "prolonged": True,
                "duration_s": 6,
            }
        )

        self.processor.process(message)

        row = self.dao.get_recent_events(1)[0]
        self.assertEqual(0x35, row["flags"])
        self.assertEqual("unknown", row["cough_type"])
        self.assertEqual(1, row["timestamp_valid"])
        self.assertEqual(0, row["stage2_valid"])
        self.assertEqual(1, row["prolonged"])
        self.assertEqual(6, row["duration_s"])

    def test_counter_reset_does_not_report_uint16_sized_gap(self) -> None:
        self.processor.process(self.cough_message("aa:bb:cc:dd:ee:01", "m-10", 100))
        self.processor.process(self.cough_message("aa:bb:cc:dd:ee:01", "m-11", 1))
        self.assertEqual(2, len(self.dao.get_recent_events(10)))

    def test_reconnect_rejects_duplicate_replay_and_accepts_next_counter(self) -> None:
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
        self.processor.process(self.cough_message(device, "m-replayed", 7))
        self.processor.process(self.cough_message(device, "m-after-reconnect", 8))
        self.assertEqual(2, len(self.dao.get_recent_events(10)))

    def test_uint16_counter_wrap_is_not_treated_as_reset(self) -> None:
        device = "aa:bb:cc:dd:ee:01"
        self.processor.process(self.cough_message(device, "m-65535", 65535))
        self.processor.process(self.cough_message(device, "m-0", 0))
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
                self.assertIn("flags", columns)
                self.assertIn("timestamp_valid", columns)
                self.assertIn("stage2_valid", columns)
                self.assertIn("prolonged", columns)
                self.assertIn("duration_s", columns)
                settings_tables = {
                    item[0]
                    for item in dao._get_conn().execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("client_settings", settings_tables)
                indexes = {
                    item[1]
                    for item in dao._get_conn().execute("PRAGMA index_list(cough_events)")
                }
                self.assertIn("ix_cough_client_event", indexes)
                self.assertEqual([], dao.get_recent_environment(10))
            finally:
                dao.close()


class AnalyticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.dao = Dao(str(Path(self.tempdir.name) / "analytics.db"))
        self.dao.init_db(str(SCHEMA))
        self.analytics = Analytics(self.dao)
        self.device_id = "aa:bb:cc:dd:ee:90"
        self.client_id = "client_analytics"
        self.dao.upsert_device(self.device_id, client_id=self.client_id)
        self.counter = 0

    def tearDown(self) -> None:
        self.dao.close()
        self.tempdir.cleanup()

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def _insert(
        self,
        occurred: datetime,
        received: datetime,
        cough_type: str = "dry",
        client_id: str | None = None,
    ) -> None:
        self.counter += 1
        target_client = client_id or self.client_id
        self.dao.insert_event(
            {
                "message_id": f"analytics-{target_client}-{self.counter}",
                "session_id": "analytics-session",
                "device_id": self.device_id,
                "client_id": target_client,
                "cough_type": cough_type,
                "event_ts": self._iso(occurred),
                "received_ts": self._iso(received),
                "event_counter": self.counter,
                "flags": 0,
                "duration_s": 2,
                "prolonged": False,
            }
        )

    def test_occurrence_time_drives_ranges_replay_and_day_night(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        # 07:30 local (day) and 03:00 local (night), both inside last 24 h.
        self._insert(now - timedelta(hours=11, minutes=30), now, "wet")
        self._insert(now - timedelta(hours=16), now, "unknown")
        # Received now after replay, but occurred four days ago: not in 24 h.
        self._insert(now - timedelta(days=4), now, "dry")

        stats = self.analytics.get_client_stats(self.client_id, now=now)

        self.assertEqual(2, stats["last_24h_count"])
        # The seven-day view contains completed local calendar days only, so
        # today's two bouts do not appear there.
        self.assertEqual(1, stats["last_7d_count"])
        self.assertEqual(
            {"wet": 1, "dry": 0, "unknown": 1}, stats["by_type_24h"]
        )
        self.assertEqual(
            {"wet": 0, "dry": 1, "unknown": 0}, stats["by_type_7d"]
        )
        self.assertEqual({"day": 1, "night": 1}, stats["day_night_24h"])

    def test_24h_trend_uses_stacked_clock_aligned_thirty_minute_buckets(self) -> None:
        now = datetime(2026, 8, 14, 4, 7, tzinfo=timezone.utc)
        # 11:01 and 11:07 local share one bucket; 10:59 is the prior bucket.
        self._insert(now - timedelta(minutes=8), now - timedelta(minutes=8), "dry")
        self._insert(now - timedelta(minutes=6), now - timedelta(minutes=6), "wet")
        self._insert(now, now, "unknown")

        stats = self.analytics.get_client_stats(self.client_id, now=now)

        nonzero = [item for item in stats["per_30_minute"] if item["total"]]
        self.assertEqual(2, len(nonzero))
        self.assertIn("T10:30:00+0700", nonzero[0]["ts"])
        self.assertEqual(
            {"dry": 1, "wet": 0, "unknown": 0, "total": 1},
            {key: nonzero[0][key] for key in ("dry", "wet", "unknown", "total")},
        )
        self.assertIn("T11:00:00+0700", nonzero[1]["ts"])
        self.assertEqual(
            {"dry": 0, "wet": 1, "unknown": 1, "total": 2},
            {key: nonzero[1][key] for key in ("dry", "wet", "unknown", "total")},
        )
        self.assertIn(len(stats["per_30_minute"]), (48, 49))

    def test_default_24h_window_uses_wall_clock_not_latest_event(self) -> None:
        now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
        last_event = now - timedelta(hours=2)
        delayed_reconnect = now
        self._insert(now - timedelta(hours=26), delayed_reconnect, "dry")
        self._insert(now - timedelta(hours=3), now - timedelta(hours=2), "wet")
        self._insert(last_event, now - timedelta(hours=1), "dry")

        stats = self.analytics.get_client_stats(self.client_id, now=now)

        self.assertEqual(self._iso(now), stats["analysis_anchor_ts"])
        self.assertEqual(self._iso(now - timedelta(hours=24)), stats["window_24h_start"])
        self.assertEqual(self._iso(now), stats["window_24h_end"])
        self.assertEqual(self._iso(last_event), stats["last_event_ts"])
        self.assertEqual(self._iso(delayed_reconnect), stats["last_received_ts"])
        self.assertEqual(2, stats["last_24h_count"])
        self.assertEqual(2, sum(item["count"] for item in stats["per_hour_history"]))

        live_feed = self.dao.get_events_by_occurrence(
            self.client_id, limit=2, descending=True
        )
        self.assertEqual(
            [self._iso(last_event), self._iso(last_event - timedelta(hours=1))],
            [event["event_ts"] for event in live_feed],
        )

    def test_occurrence_event_pagination_is_newest_first_and_counted(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        for offset in range(6):
            occurred = now - timedelta(minutes=offset)
            self._insert(occurred, occurred)

        second_page = self.dao.get_events_by_occurrence(
            self.client_id,
            limit=2,
            descending=True,
            offset=2,
        )

        self.assertEqual(6, self.dao.count_events_by_occurrence(self.client_id))
        self.assertEqual(
            [self._iso(now - timedelta(minutes=2)), self._iso(now - timedelta(minutes=3))],
            [event["event_ts"] for event in second_page],
        )

    def test_seven_day_view_uses_completed_days_and_day_starts_at_six(self) -> None:
        now = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)  # local noon
        for offset in range(7, 0, -1):
            local_day = now.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=offset)
            day_event = datetime.combine(
                local_day, time(6, 0), ZoneInfo("Asia/Ho_Chi_Minh")
            )
            night_event = datetime.combine(
                local_day, time(22, 0), ZoneInfo("Asia/Ho_Chi_Minh")
            )
            self._insert(day_event, day_event, "dry")
            self._insert(night_event, night_event, "wet")
        self._insert(now, now, "unknown")  # today is excluded from 7d

        stats = self.analytics.get_client_stats(self.client_id, now=now)

        self.assertTrue(stats["completed_7d_available"])
        self.assertEqual(7, len(stats["per_day"]))
        self.assertEqual(14, stats["last_7d_count"])
        # The prior completed day's 22:00 night event is also within the
        # rolling wall-clock window.
        self.assertEqual(2, stats["last_24h_count"])
        self.assertTrue(all(item["day"] == 1 for item in stats["per_day"]))
        self.assertTrue(all(item["night"] == 1 for item in stats["per_day"]))
        self.assertTrue(
            all(item["day_types"]["dry"] == 1 for item in stats["per_day"])
        )
        self.assertTrue(
            all(item["night_types"]["wet"] == 1 for item in stats["per_day"])
        )

    def test_ewma_warmup_threshold_and_continuing_update(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        local_day_anchor_utc = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)
        counts = [10, 20, 30, 40, 50, 60, 70]
        for day_offset, count in enumerate(counts):
            occurred = local_day_anchor_utc + timedelta(days=day_offset)
            for _ in range(count):
                self._insert(occurred, occurred + timedelta(seconds=1))
        for _ in range(60):
            self._insert(now - timedelta(hours=1), now)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertTrue(status["available"])
        self.assertEqual(7, status["observed_history_days"])
        self.assertEqual(0.2, status["alpha"])
        self.assertEqual(40.49, status["baseline"])
        self.assertEqual(56.68, status["threshold"])
        self.assertTrue(status["above_baseline"])

    def test_missing_day_is_not_zero_or_a_warmup_day(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        for day_offset in (1, 2, 3, 5, 6, 7):
            occurred = now - timedelta(days=day_offset, hours=1)
            self._insert(occurred, occurred + timedelta(seconds=1))

        daily = self.analytics.daily_cough_counts(self.client_id, now=now)
        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertEqual(6, len(daily))
        self.assertFalse(status["available"])
        self.assertEqual("warmup", status["reason"])
        self.assertEqual(1, status["warmup_remaining"])

    def test_seven_identical_days_produce_exact_ewma(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        for offset in range(7, 0, -1):
            occurred = now - timedelta(days=offset, hours=1)
            for _ in range(5):
                self._insert(occurred, occurred)
        status = self.analytics.ewma_baseline_status(self.client_id, now=now)
        self.assertTrue(status["available"])
        self.assertEqual(5.0, status["baseline"])
        self.assertEqual(10.0, status["threshold"])

    def test_abnormal_completed_day_uses_reduced_ewma_alpha(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        counts = [20] * 7 + [100]
        for index, count in enumerate(counts):
            occurred = now - timedelta(days=len(counts) - index, hours=1)
            for _ in range(count):
                self._insert(occurred, occurred)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertEqual(24.0, status["baseline"])
        self.assertNotEqual(36.0, status["baseline"])
        self.assertEqual(1, status["consecutive_abnormal_days"])

    def test_normal_completed_day_keeps_standard_ewma_alpha(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        counts = [20] * 7 + [22]
        for index, count in enumerate(counts):
            occurred = now - timedelta(days=len(counts) - index, hours=1)
            for _ in range(count):
                self._insert(occurred, occurred)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertEqual(20.4, status["baseline"])
        self.assertEqual(0, status["consecutive_abnormal_days"])

    def test_current_day_spike_does_not_update_personal_baseline(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        for offset in range(7, 0, -1):
            occurred = now - timedelta(days=offset, hours=1)
            for _ in range(20):
                self._insert(occurred, occurred)
        for _ in range(100):
            self._insert(now - timedelta(hours=1), now)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertEqual(20.0, status["baseline"])
        self.assertEqual(100, status["c24"])
        self.assertEqual("high_priority", status["warning_level"])
        self.assertEqual("High Alert", status["warning_label"])

    def test_repeated_abnormal_days_adapt_baseline_slowly(self) -> None:
        now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        counts = [20] * 7 + [100, 100, 100]
        for index, count in enumerate(counts):
            occurred = now - timedelta(days=len(counts) - index, hours=1)
            for _ in range(count):
                self._insert(occurred, occurred)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertEqual(31.41, status["baseline"])
        self.assertEqual(3, status["consecutive_abnormal_days"])

    def test_warning_normal_review_and_ratio_escalation(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        scenarios = {
            "warning_normal": (20, 28, "normal", "Normal"),
            "warning_review": (20, 29, "needs_review", "Warning"),
            "warning_ratio": (5, 11, "high_priority", "High Alert"),
        }
        for client_id, (historical, current, expected, label) in scenarios.items():
            for offset in range(7, 0, -1):
                occurred = now - timedelta(days=offset, hours=1)
                for _ in range(historical):
                    self._insert(occurred, occurred, client_id=client_id)
            for _ in range(current):
                self._insert(now - timedelta(hours=1), now, client_id=client_id)
            status = self.analytics.ewma_baseline_status(client_id, now=now)
            self.assertEqual(current, status["c24"])
            self.assertEqual(expected, status["warning_level"])
            self.assertEqual(label, status["warning_label"])

    def test_client_baselines_are_isolated(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        other_client = "client_other"
        for day_offset in range(1, 8):
            occurred = now - timedelta(days=day_offset, hours=1)
            self._insert(occurred, occurred, client_id=other_client)

        own = self.analytics.ewma_baseline_status(self.client_id, now=now)
        other = self.analytics.ewma_baseline_status(other_client, now=now)

        self.assertFalse(own["available"])
        self.assertTrue(other["available"])
        self.assertEqual(1.0, other["baseline"])

    def test_treatment_date_is_stored_per_patient_and_can_be_cleared(self) -> None:
        saved = self.dao.set_treatment_start_date(self.client_id, "2026-08-09")
        self.assertEqual("2026-08-09", saved["treatment_start_date"])
        self.assertIsNone(
            self.dao.get_client_settings("another_patient")["treatment_start_date"]
        )

        cleared = self.dao.set_treatment_start_date(self.client_id, None)
        self.assertIsNone(cleared["treatment_start_date"])

    def test_treatment_response_uses_automatic_completed_weeks(self) -> None:
        for day_number in range(1, 8):
            occurred = datetime(2026, 8, day_number, 5, 0, tzinfo=timezone.utc)
            for _ in range(10):
                self._insert(occurred, occurred)
        for day_number in range(8, 15):
            occurred = datetime(2026, 8, day_number, 5, 0, tzinfo=timezone.utc)
            for _ in range(5):
                self._insert(occurred, occurred)

        status = self.analytics.treatment_response_status(
            self.client_id,
            now=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(status["available"])
        self.assertEqual("2026-08-01", status["first_data_date"])
        self.assertEqual(2, status["evaluation_week_number"])
        self.assertEqual(7, status["reference_observed_days"])
        self.assertEqual(10.0, status["ewma_reference"])
        self.assertEqual(5.0, status["current"])
        self.assertEqual(-50.0, status["change_percent"])
        self.assertEqual("decreased", status["direction"])

    def test_treatment_response_does_not_invent_zero_days(self) -> None:
        first = datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
        self._insert(first, first)
        day_two = datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc)
        for _ in range(7):
            self._insert(day_two, day_two)

        status = self.analytics.treatment_response_status(
            self.client_id,
            now=datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(status["available"])
        self.assertEqual("reference_warmup", status["reason"])
        self.assertEqual(2, status["reference_observed_days"])

    def test_treatment_response_keeps_separate_seven_day_warmup(self) -> None:
        for day_number in range(1, 6):
            occurred = datetime(
                2026, 8, day_number, 5, 0, tzinfo=timezone.utc
            )
            self._insert(occurred, occurred)

        status = self.analytics.treatment_response_status(
            self.client_id,
            now=datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(status["available"])
        self.assertEqual("warmup", status["reason"])
        self.assertEqual(5, status["reference_observed_days"])
        self.assertEqual(2, status["warmup_remaining"])

    def test_event_dates_include_only_dates_that_have_events(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        self._insert(now - timedelta(days=4), now - timedelta(days=4))
        self._insert(now - timedelta(days=1), now - timedelta(days=1))
        payload = self.analytics.event_dates(self.client_id)
        self.assertEqual(2, len(payload["dates"]))
        self.assertEqual(payload["dates"][0], payload["first_date"])
        self.assertEqual(payload["dates"][-1], payload["last_date"])

    def test_warning_uses_c24_and_pre_update_abnormal_streak(self) -> None:
        now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        for offset in range(9, 2, -1):
            occurred = now - timedelta(days=offset, hours=1)
            for _ in range(5):
                self._insert(occurred, occurred)
        for offset in (2, 1):
            occurred = now - timedelta(days=offset, hours=1)
            for _ in range(14):
                self._insert(occurred, occurred)
        for _ in range(20):
            self._insert(now - timedelta(hours=1), now)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)
        self.assertEqual(20, status["c24"])
        self.assertEqual(2, status["consecutive_abnormal_days"])
        self.assertEqual("high_priority", status["warning_level"])
        self.assertEqual("High Alert", status["warning_label"])


class SimulatedDataTest(unittest.TestCase):
    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def test_simulator_is_reproducible_isolated_and_supports_live_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            dao = Dao(str(Path(tempdir) / "sim.db"))
            dao.init_db(str(SCHEMA))
            try:
                now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
                first = simulate_history(dao, now=now, seed=17)
                analytics = Analytics(dao)
                self.assertTrue(first["created"])
                self.assertEqual(6, len(first["patients"]))
                self.assertTrue(
                    all(client.startswith(SIM_PREFIX) for client in first["patients"])
                )
                worsening = analytics.get_client_stats(
                    f"{SIM_PREFIX}worsening", now=now
                )
                warmup = analytics.get_client_stats(f"{SIM_PREFIX}warmup", now=now)
                stable = analytics.get_client_stats(f"{SIM_PREFIX}stable", now=now)
                review = analytics.get_client_stats(
                    f"{SIM_PREFIX}needs-review", now=now
                )
                treatment = analytics.get_client_stats(
                    f"{SIM_PREFIX}treatment-improving", now=now
                )
                self.assertEqual("normal", stable["baseline"]["warning_level"])
                self.assertEqual(
                    "needs_review", review["baseline"]["warning_level"]
                )
                self.assertEqual(
                    "normal", treatment["baseline"]["warning_level"]
                )
                self.assertTrue(worsening["baseline"]["available"])
                self.assertIn(
                    worsening["baseline"]["warning_level"],
                    {"needs_review", "high_priority"},
                )
                self.assertFalse(warmup["baseline"]["available"])

                stable_client = PROFILES[0].client_id
                signature = [
                    (
                        row["event_ts"],
                        row["cough_type"],
                        row["prolonged"],
                        row["duration_s"],
                    )
                    for row in dao.get_events(stable_client)
                ]

                duplicate = simulate_history(dao, now=now, seed=17)
                self.assertFalse(duplicate["created"])

                legacy_client = LEGACY_DEMO_CLIENT_IDS[-1]
                legacy_device = LEGACY_DEMO_DEVICE_IDS[-1]
                dao.upsert_device(
                    legacy_device,
                    name="Retired demo sensor",
                    address_type=0,
                    client_id=legacy_client,
                    status="offline",
                    last_seen="2026-08-20T00:00:00.000Z",
                )
                dao.insert_event(
                    {
                        "message_id": "demo-dashboard-retired-event",
                        "session_id": "retired-demo",
                        "device_id": legacy_device,
                        "client_id": legacy_client,
                        "cough_type": "dry",
                        "event_ts": "2026-08-20T00:00:00.000Z",
                        "received_ts": "2026-08-20T00:00:01.000Z",
                    }
                )
                dao.upsert_device(
                    "real-device",
                    name="Real sensor",
                    address_type=0,
                    client_id="real-patient",
                    status="online",
                    last_seen="2026-08-23T12:00:00.000Z",
                )
                dao.insert_event(
                    {
                        "message_id": "real-event",
                        "session_id": "real-session",
                        "device_id": "real-device",
                        "client_id": "real-patient",
                        "cough_type": "wet",
                        "event_ts": "2026-08-23T11:00:00.000Z",
                        "received_ts": "2026-08-23T11:00:01.000Z",
                    }
                )
                replaced = simulate_history(dao, now=now, seed=17, replace=True)
                self.assertTrue(replaced["created"])
                self.assertEqual(first["events"], replaced["events"])
                self.assertEqual(
                    signature,
                    [
                        (
                            row["event_ts"],
                            row["cough_type"],
                            row["prolonged"],
                            row["duration_s"],
                        )
                        for row in dao.get_events(stable_client)
                    ],
                )
                self.assertFalse(dao.get_events(legacy_client))
                self.assertEqual(1, len(dao.get_events("real-patient")))
                device_ids = {row["device_id"] for row in dao.get_devices()}
                self.assertNotIn(legacy_device, device_ids)
                self.assertIn("real-device", device_ids)

                profile = PROFILES[0]
                before = len(dao.get_events(profile.client_id))
                self.assertTrue(
                    insert_live_event(
                        dao, profile, random.Random(99), 1, now=now
                    )
                )
                self.assertEqual(before + 1, len(dao.get_events(profile.client_id)))

                event_count = len(dao.get_recent_events(500))
                environment_count = len(dao.get_recent_environment(500))
                dao.mark_stale_devices_offline(
                    "2026-08-23T12:00:01.000Z"
                )
                self.assertTrue(
                    all(
                        row["status"] == "offline"
                        for row in dao.get_devices()
                        if row["device_id"].startswith(SIM_PREFIX)
                    )
                )
                heartbeat_at = now + timedelta(seconds=10)
                self.assertEqual(
                    len(PROFILES),
                    refresh_simulated_device_status(dao, now=heartbeat_at),
                )
                simulated_devices = [
                    row
                    for row in dao.get_devices()
                    if row["device_id"].startswith(SIM_PREFIX)
                ]
                self.assertTrue(
                    all(row["status"] == "online" for row in simulated_devices)
                )
                self.assertTrue(
                    all(
                        row["last_seen"] == self._iso(heartbeat_at)
                        for row in simulated_devices
                    )
                )
                self.assertEqual(event_count, len(dao.get_recent_events(500)))
                self.assertEqual(
                    environment_count,
                    len(dao.get_recent_environment(500)),
                )
                cleanup_simulated_data(dao)
                self.assertFalse(
                    any(
                        row["device_id"].startswith(SIM_PREFIX)
                        for row in dao.get_devices()
                    )
                )
            finally:
                dao.close()

    def test_live_caps_preserve_each_simulated_warning_scenario(self) -> None:
        expected_levels = {
            "stable": "normal",
            "needs-review": "needs_review",
            "worsening": "high_priority",
            "treatment-improving": "normal",
            "warmup": "calibrating",
            "irregular-missing": "normal",
        }
        with tempfile.TemporaryDirectory() as tempdir:
            dao = Dao(str(Path(tempdir) / "sim-caps.db"))
            dao.init_db(str(SCHEMA))
            try:
                now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
                simulate_history(dao, now=now, seed=17)
                analytics = Analytics(dao)
                serial = 1000
                for profile in PROFILES:
                    for _ in range(100):
                        serial += 1
                        insert_live_event(
                            dao,
                            profile,
                            random.Random(serial),
                            serial,
                            now=now,
                        )
                    current_c24 = dao.count_events_by_occurrence(
                        profile.client_id,
                        start_time=self._iso(now - timedelta(hours=24)),
                        end_time=self._iso(now),
                    )
                    self.assertLessEqual(current_c24, profile.live_c24_cap)
                    self.assertFalse(
                        insert_live_event(
                            dao,
                            profile,
                            random.Random(serial + 1),
                            serial + 1,
                            now=now,
                        )
                    )
                    status = analytics.ewma_baseline_status(
                        profile.client_id,
                        now=now,
                    )
                    self.assertEqual(
                        expected_levels[profile.key],
                        status["warning_level"],
                        profile.key,
                    )
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
        class FakeCommandFailedError(Exception):
            def __init__(self, errorcode: int = 1) -> None:
                super().__init__(errorcode)
                self.errorcode = errorcode

        fake_bgapi = types.ModuleType("bgapi")
        fake_connector = types.ModuleType("bgapi.connector")
        fake_connector.ConnectorException = type(
            "ConnectorException", (Exception,), {}
        )
        fake_bgapi.bglib = SimpleNamespace(
            CommandFailedError=FakeCommandFailedError,
        )
        fake_bgapi.connector = fake_connector
        sys.modules.setdefault("bgapi", fake_bgapi)
        sys.modules.setdefault("bgapi.connector", fake_connector)
        sys.path.insert(0, str(ROOT / "gateway" / "ble_host_modular"))

    def test_connector_open_error_is_reported_without_secondary_failure(self) -> None:
        from bgapi.connector import ConnectorException
        from ble_central import BleCentral, NcpTransportLost

        class UnavailableNcp:
            @staticmethod
            def open() -> None:
                raise ConnectorException("serial port is unavailable")

        central = BleCentral.__new__(BleCentral)
        central.lib = UnavailableNcp()
        central.args = SimpleNamespace(serial_port="/dev/ttyACM0")

        with self.assertRaisesRegex(
            NcpTransportLost, "Cannot open BGM220 NCP at /dev/ttyACM0"
        ):
            central.run()

        self.assertTrue(central._closed)

    def test_dead_bgapi_reader_is_not_a_live_transport(self) -> None:
        from ble_central import BleCentral

        central = BleCentral.__new__(BleCentral)
        central.lib = SimpleNamespace(
            conn_handler=SimpleNamespace(is_alive=lambda: False)
        )

        self.assertFalse(central._ncp_transport_alive())

    def test_transport_loss_reports_every_running_node_offline_and_cleans_up(self) -> None:
        from ble_central import BleCentral, NcpTransportLost
        from models import ConnectionState

        first = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            phase="running",
            status_reported=True,
        )
        second = ConnectionState(
            2,
            "aa:bb:cc:dd:ee:02",
            0,
            "MyDevice_02",
            phase="running",
            status_reported=True,
        )
        statuses: list[tuple[str, str]] = []
        closed_handles: list[int] = []
        backend_closed: list[bool] = []
        library_closed: list[bool] = []
        central = BleCentral.__new__(BleCentral)
        central.args = SimpleNamespace(
            serial_port="/dev/ttyACM0",
            max_connections=2,
        )
        central.session_id = "transport-loss-test"
        central.booted = True
        central.scanning = False
        central.pending_node = None
        central.connections = {1: first, 2: second}
        central.connection_by_address = {
            first.address: first.handle,
            second.address: second.handle,
        }
        central._closed = False
        central._emit_status = lambda state, status: statuses.append(
            (state.address, status)
        )
        central.backend = SimpleNamespace(
            enabled=False,
            close=lambda: backend_closed.append(True),
        )
        central.lib = SimpleNamespace(
            open=lambda: None,
            conn_handler=SimpleNamespace(is_alive=lambda: False),
            bt=SimpleNamespace(
                system=SimpleNamespace(reboot=lambda: None),
                connection=SimpleNamespace(
                    close=lambda handle: closed_handles.append(handle)
                ),
            ),
            close=lambda: library_closed.append(True),
        )

        with self.assertRaisesRegex(NcpTransportLost, "transport lost"):
            central.run()

        self.assertEqual(
            {
                (first.address, "disconnected"),
                (second.address, "disconnected"),
            },
            set(statuses),
        )
        self.assertEqual([1, 2], closed_handles)
        self.assertEqual({}, central.connections)
        self.assertEqual({}, central.connection_by_address)
        self.assertEqual([True], backend_closed)
        self.assertEqual([True], library_closed)

    def test_supervisor_recreates_central_until_transport_returns(self) -> None:
        from ble_central import NcpTransportLost
        from constants import NCP_RETRY_SECONDS
        from main import supervise

        created: list[int] = []
        sleeps: list[float] = []

        class RecoveringCentral:
            def __init__(self, _args) -> None:
                self.attempt = len(created) + 1
                created.append(self.attempt)

            def run(self) -> None:
                if self.attempt < 3:
                    raise NcpTransportLost(f"attempt {self.attempt}")

        supervise(
            SimpleNamespace(),
            central_factory=RecoveringCentral,
            sleep=sleeps.append,
        )

        self.assertEqual([1, 2, 3], created)
        self.assertEqual([NCP_RETRY_SECONDS, NCP_RETRY_SECONDS], sleeps)

    def test_supervisor_ctrl_c_exits_without_restart(self) -> None:
        from main import supervise

        created: list[bool] = []
        sleeps: list[float] = []

        class InterruptedCentral:
            def __init__(self, _args) -> None:
                created.append(True)

            @staticmethod
            def run() -> None:
                raise KeyboardInterrupt

        supervise(
            SimpleNamespace(),
            central_factory=InterruptedCentral,
            sleep=sleeps.append,
        )

        self.assertEqual([True], created)
        self.assertEqual([], sleeps)

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

    def test_clean_ble_host_shutdown_reports_running_nodes_offline(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        reported = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            phase="running",
            status_reported=True,
        )
        unreported = ConnectionState(
            2,
            "aa:bb:cc:dd:ee:02",
            0,
            "MyDevice_02",
            phase="discover_characteristics",
            status_reported=False,
        )
        statuses: list[tuple[str, str]] = []
        closed_handles: list[int] = []
        backend_closed: list[bool] = []
        library_closed: list[bool] = []
        central = BleCentral.__new__(BleCentral)
        central.connections = {1: reported, 2: unreported}
        central.connection_by_address = {
            reported.address: 1,
            unreported.address: 2,
        }
        central.pending_node = None
        central.stop_scan = lambda: None
        central._emit_status = lambda state, status: statuses.append(
            (state.address, status)
        )
        central.backend = SimpleNamespace(
            close=lambda: backend_closed.append(True)
        )
        central.lib = SimpleNamespace(
            bt=SimpleNamespace(
                connection=SimpleNamespace(
                    close=lambda handle: closed_handles.append(handle)
                )
            ),
            close=lambda: library_closed.append(True),
        )

        central.close()
        central.close()

        self.assertEqual([(reported.address, "disconnected")], statuses)
        self.assertFalse(reported.status_reported)
        self.assertEqual([1, 2], closed_handles)
        self.assertEqual({}, central.connections)
        self.assertEqual([True], backend_closed)
        self.assertEqual([True], library_closed)

    def test_status_heartbeat_is_per_running_connection(self) -> None:
        from ble_central import BleCentral
        from constants import DEVICE_STATUS_HEARTBEAT_SECONDS
        from models import ConnectionState

        due = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            phase="running",
            status_reported=True,
            last_status_heartbeat_at=10.0,
        )
        recent = ConnectionState(
            2,
            "aa:bb:cc:dd:ee:02",
            0,
            "MyDevice_02",
            phase="running",
            status_reported=True,
            last_status_heartbeat_at=19.0,
        )
        discovering = ConnectionState(
            3,
            "aa:bb:cc:dd:ee:03",
            0,
            "MyDevice_03",
            phase="discover_characteristics",
            status_reported=True,
        )
        unreported = ConnectionState(
            4,
            "aa:bb:cc:dd:ee:04",
            0,
            "MyDevice_04",
            phase="running",
            status_reported=False,
        )
        central = BleCentral.__new__(BleCentral)
        central.lib = SimpleNamespace(
            conn_handler=SimpleNamespace(is_alive=lambda: True)
        )
        central.connections = {
            state.handle: state
            for state in (due, recent, discovering, unreported)
        }
        emitted: list[str] = []
        central._emit_status_heartbeat = lambda state: emitted.append(state.address)

        central._check_status_heartbeats(
            10.0 + DEVICE_STATUS_HEARTBEAT_SECONDS
        )

        self.assertEqual([due.address], emitted)
        self.assertEqual(20.0, due.last_status_heartbeat_at)
        self.assertEqual(19.0, recent.last_status_heartbeat_at)

    def test_dead_transport_suppresses_connected_heartbeats(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        state = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            phase="running",
            status_reported=True,
            last_status_heartbeat_at=0.0,
        )
        central = BleCentral.__new__(BleCentral)
        central.lib = SimpleNamespace(
            conn_handler=SimpleNamespace(is_alive=lambda: False)
        )
        central.connections = {state.handle: state}
        emitted: list[str] = []
        central._emit_status_heartbeat = lambda item: emitted.append(item.address)

        central._check_status_heartbeats(100.0)

        self.assertEqual([], emitted)
        self.assertEqual(0.0, state.last_status_heartbeat_at)

    def test_status_heartbeat_uses_best_effort_transport(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        state = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            phase="running",
            status_reported=True,
        )
        sent: list[dict] = []
        central = BleCentral.__new__(BleCentral)
        central.session_id = "heartbeat-test"
        central.sequence = 0
        central.backend = SimpleNamespace(
            enabled=True,
            send_best_effort=lambda message: sent.append(message) or True,
        )

        central._emit_status_heartbeat(state)

        self.assertEqual(1, len(sent))
        self.assertEqual("status", sent[0]["event"])
        self.assertEqual("connected", sent[0]["status"])
        self.assertTrue(sent[0]["heartbeat"])

    def test_best_effort_heartbeat_never_enters_retry_fifo(self) -> None:
        from backend_client import JsonLineBackend

        backend = JsonLineBackend("/tmp/unavailable-breathsense-test.sock")
        backend._send_encoded = lambda _encoded: False

        for sequence in range(20):
            self.assertFalse(
                backend.send_best_effort({"event": "status", "sequence": sequence})
            )

        self.assertEqual(0, backend.queued_count)
        backend._queue.append(b'{"event":"cough_event"}\n')
        self.assertFalse(
            backend.send_best_effort({"event": "status", "heartbeat": True})
        )
        self.assertEqual(1, backend.queued_count)

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

    def test_cough_payload_keeps_eight_byte_contract_and_uses_node_epoch(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        epoch = 1785369600
        payload = struct.pack("<BBIH", 3, 2, epoch, 65535)
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
                phase="running",
            )
        }
        central.session_id = "test-session"
        central.sequence = 0
        central.backend = SimpleNamespace(
            enabled=True,
            send=lambda message: sent.append(message) or True,
        )
        central.lib = SimpleNamespace(gatt=SimpleNamespace())

        central.on_gatt_characteristic_value(
            SimpleNamespace(
                connection=1,
                characteristic=20,
                att_opcode=0x1B,
                value=payload,
            )
        )

        self.assertEqual(8, len(payload))
        self.assertEqual(payload.hex(), sent[0]["payload_hex"])
        self.assertEqual(epoch, sent[0]["parsed"]["event_timestamp"])
        self.assertEqual(65535, sent[0]["parsed"]["event_counter"])
        self.assertEqual("node_unix_seconds", sent[0]["parsed"]["timestamp_source"])
        self.assertEqual("2026-07-30T00:00:00.000Z", sent[0]["event_ts"])

    def test_zero_node_epoch_falls_back_to_received_time(self) -> None:
        from utils import resolve_event_timestamp

        received = "2026-07-30T02:00:00.123Z"
        self.assertEqual(
            (received, "gateway_received"),
            resolve_event_timestamp(0, received),
        )

    def test_any_positive_uint32_node_epoch_is_used(self) -> None:
        from utils import resolve_event_timestamp

        self.assertEqual(
            ("1970-01-01T00:00:01.000Z", "node_unix_seconds"),
            resolve_event_timestamp(1, "2026-07-30T02:00:00.123Z"),
        )

    def test_cough_flags_decode_bout_metadata_without_changing_wire_size(self) -> None:
        from payload_parser import COUGH_EVENT_STRUCT, parse_cough_payload

        payload = struct.pack("<BBIH", 0x35, 0, 1785369600, 2)
        parsed = parse_cough_payload(payload)

        self.assertEqual(8, COUGH_EVENT_STRUCT.size)
        self.assertIsNotNone(parsed)
        self.assertEqual(
            {
                "flags": 0x35,
                "timestamp_valid": True,
                "stage2_valid": False,
                "prolonged": True,
                "duration_s": 6,
                "cough_type": 0,
                "cough_type_name": "unknown",
                "event_timestamp": 1785369600,
                "event_counter": 2,
            },
            parsed,
        )


class BleTimeSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        BleRoutingTest.setUpClass()

    @staticmethod
    def _central_with_states(states):
        from ble_central import BleCentral

        central = BleCentral.__new__(BleCentral)
        central.connections = {state.handle: state for state in states}
        central.utc_sync_date = date(2026, 7, 29)
        central.start_scan = lambda: None
        central._emit_status = lambda state, status: None
        return central

    def test_time_characteristic_is_discovered_per_connection(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState
        from utils import uuid_to_bgapi_bytes

        first = ConnectionState(1, "aa:bb:cc:dd:ee:01", 0, "MyDevice_01")
        second = ConnectionState(2, "aa:bb:cc:dd:ee:02", 0, "MyDevice_02")
        central = self._central_with_states([first, second])
        central.target_cough_uuid = b"cough"
        central.target_environment_uuid = b"environment"
        central.target_time_uuid = uuid_to_bgapi_bytes(
            "b5e00004-7a4b-4c6d-9e10-112233445566"
        )

        central.on_gatt_characteristic(
            SimpleNamespace(
                connection=1,
                characteristic=22,
                properties=0x08,
                uuid=central.target_time_uuid,
            )
        )

        self.assertEqual(22, first.time_characteristic)
        self.assertEqual(0x08, first.time_characteristic_properties)
        self.assertIsNone(second.time_characteristic)

    def test_connect_enables_legacy_node_without_time_write(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        state = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            phase="enable_environment_notifications",
        )
        central = self._central_with_states([state])
        central.lib = SimpleNamespace(bt=SimpleNamespace(gatt=SimpleNamespace()))

        BleCentral.on_gatt_procedure_completed(
            central, SimpleNamespace(connection=1, result=0)
        )

        self.assertEqual("running", state.phase)
        self.assertTrue(state.status_reported)
        self.assertIsNone(state.last_time_sync_epoch)

    def test_connect_writes_little_endian_uint32_time_after_notifications(self) -> None:
        from ble_central import BleCentral
        from models import ConnectionState

        writes: list[tuple[int, int, bytes]] = []
        state = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            time_characteristic=22,
            time_characteristic_properties=0x08,
            phase="enable_environment_notifications",
        )
        central = self._central_with_states([state])
        central.lib = SimpleNamespace(
            bt=SimpleNamespace(
                gatt=SimpleNamespace(
                    write_characteristic_value=lambda *args: writes.append(args)
                )
            )
        )

        BleCentral.on_gatt_procedure_completed(
            central, SimpleNamespace(connection=1, result=0)
        )
        self.assertEqual("sync_time_connect", state.phase)
        self.assertEqual(1, len(writes))
        self.assertEqual((1, 22), writes[0][:2])
        self.assertEqual(4, len(writes[0][2]))
        self.assertEqual(state.pending_time_sync_epoch, struct.unpack("<I", writes[0][2])[0])

        BleCentral.on_gatt_procedure_completed(
            central, SimpleNamespace(connection=1, result=0)
        )
        self.assertEqual("running", state.phase)
        self.assertEqual("connect", state.last_time_sync_reason)

    def test_utc_date_change_resyncs_nodes_once_and_isolates_failure(self) -> None:
        import bgapi
        from models import ConnectionState

        failed = ConnectionState(
            1,
            "aa:bb:cc:dd:ee:01",
            0,
            "MyDevice_01",
            time_characteristic=22,
            time_characteristic_properties=0x08,
            phase="running",
            status_reported=True,
        )
        healthy = ConnectionState(
            2,
            "aa:bb:cc:dd:ee:02",
            0,
            "MyDevice_02",
            time_characteristic=22,
            time_characteristic_properties=0x08,
            phase="running",
            status_reported=True,
        )
        legacy = ConnectionState(
            3,
            "aa:bb:cc:dd:ee:03",
            0,
            "MyDevice_03",
            phase="running",
            status_reported=True,
        )
        writes: list[tuple[int, int, bytes]] = []

        def write(connection, characteristic, value):
            if connection == 1:
                raise bgapi.bglib.CommandFailedError(0x0180)
            writes.append((connection, characteristic, value))

        central = self._central_with_states([failed, healthy, legacy])
        central.lib = SimpleNamespace(
            bt=SimpleNamespace(
                gatt=SimpleNamespace(write_characteristic_value=write)
            )
        )
        midnight = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)

        central._check_daily_time_sync(midnight)
        central._check_daily_time_sync(midnight)

        self.assertEqual("running", failed.phase)
        self.assertIsNone(failed.last_time_sync_epoch)
        self.assertEqual("sync_time_utc_midnight", healthy.phase)
        self.assertEqual("running", legacy.phase)
        self.assertEqual([(2, 22, struct.pack("<I", 1785369600))], writes)


if __name__ == "__main__":
    unittest.main()
