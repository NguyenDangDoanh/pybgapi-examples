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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.app.dao import Dao
from gateway.app.analytics import Analytics
from gateway.app.event_processor import EventProcessor
from gateway.app.fleet import Fleet
from gateway.app.seed_demo_data import (
    ABOVE_CLIENT,
    COMPLETED_CLIENT,
    WEEK2_CLIENT,
    WARMUP_CLIENT,
    seed_demo_data,
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
        self.assertEqual(3, stats["last_7d_count"])
        self.assertEqual(
            {"wet": 1, "dry": 0, "unknown": 1}, stats["by_type_24h"]
        )
        self.assertEqual(
            {"wet": 1, "dry": 1, "unknown": 1}, stats["by_type_7d"]
        )
        self.assertEqual({"day": 1, "night": 1}, stats["day_night_24h"])

    def test_live_feed_local_date_filter_uses_event_not_received_time(self) -> None:
        local_zone = ZoneInfo("Asia/Ho_Chi_Minh")
        occurred = datetime(2026, 8, 12, 23, 50, tzinfo=local_zone)
        replayed = datetime(2026, 8, 14, 9, 0, tzinfo=local_zone)
        self._insert(occurred, replayed)
        start = datetime(2026, 8, 12, 0, 0, tzinfo=local_zone)
        end = start + timedelta(days=1) - timedelta(microseconds=1)

        events = self.dao.get_events_by_occurrence(
            self.client_id,
            start_time=self._iso(start),
            end_time=self._iso(end),
            descending=True,
        )

        self.assertEqual(1, len(events))
        self.assertEqual(self._iso(occurred), events[0]["event_ts"])
        self.assertEqual(self._iso(replayed), events[0]["received_ts"])

    def test_24h_trend_uses_single_total_thirty_minute_buckets(self) -> None:
        now = datetime(2026, 8, 14, 4, 7, tzinfo=timezone.utc)
        # 11:01 and 11:07 local share one bucket; 10:59 is the prior bucket.
        self._insert(now - timedelta(minutes=8), now - timedelta(minutes=8))
        self._insert(now - timedelta(minutes=6), now - timedelta(minutes=6))
        self._insert(now, now)

        stats = self.analytics.get_client_stats(self.client_id, now=now)

        self.assertEqual(
            [1, 2],
            [item["count"] for item in stats["per_10_minute"]],
        )
        self.assertTrue(stats["per_10_minute"][0]["ts"].endswith("+0700"))
        self.assertIn("T11:00:00+0700", stats["per_10_minute"][1]["ts"])
        self.assertEqual(
            [1, 2],
            [item["count"] for item in stats["per_30_minute"]],
        )
        self.assertNotIn("wet", stats["per_30_minute"][0])

    def test_default_24h_window_ends_at_latest_event_time(self) -> None:
        last_event = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
        delayed_reconnect = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        self._insert(
            last_event - timedelta(hours=25),
            delayed_reconnect,
            "dry",
        )
        self._insert(
            last_event - timedelta(hours=1),
            last_event + timedelta(seconds=1),
            "wet",
        )
        self._insert(last_event, last_event + timedelta(seconds=2), "dry")

        stats = self.analytics.get_client_stats(self.client_id)

        self.assertEqual(self._iso(last_event), stats["analysis_anchor_ts"])
        self.assertEqual(self._iso(last_event), stats["last_event_ts"])
        self.assertEqual(self._iso(delayed_reconnect), stats["last_received_ts"])
        self.assertEqual(2, stats["last_24h_count"])
        self.assertEqual(3, sum(item["count"] for item in stats["per_hour_history"]))

        live_feed = self.dao.get_events_by_occurrence(
            self.client_id, limit=2, descending=True
        )
        self.assertEqual(
            [self._iso(last_event), self._iso(last_event - timedelta(hours=1))],
            [event["event_ts"] for event in live_feed],
        )

    def test_ewma_warmup_threshold_and_continuing_update(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        # August 5 is the automatic start marker and is conservatively partial.
        partial = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)
        self._insert(partial, partial)
        local_day_anchor_utc = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)
        counts = [10, 20, 30, 40, 50, 60, 70, 80]
        for day_offset, count in enumerate(counts):
            occurred = local_day_anchor_utc + timedelta(days=day_offset)
            for _ in range(count):
                self._insert(occurred, occurred + timedelta(seconds=1))
        for _ in range(80):
            self._insert(now - timedelta(hours=1), now)

        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertTrue(status["available"])
        self.assertEqual(8, status["observed_history_days"])
        self.assertEqual(0.2, status["alpha"])
        self.assertEqual(48.39, status["baseline"])
        self.assertEqual(67.75, status["threshold"])
        self.assertTrue(status["above_baseline"])
        timeline = self.analytics.personal_ewma_timeline(self.client_id, now=now)
        day_eight = next(item for item in timeline["days"] if item["date"] == "2026-08-13")
        self.assertEqual(40.49, day_eight["baseline_before"])
        self.assertEqual(48.39, day_eight["baseline_after"])

    def test_missing_day_is_not_zero_or_a_warmup_day(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        for day_offset in (1, 2, 3, 5, 6, 7, 8):
            occurred = now - timedelta(days=day_offset, hours=1)
            self._insert(occurred, occurred + timedelta(seconds=1))

        daily = self.analytics.daily_cough_counts(self.client_id, now=now)
        status = self.analytics.ewma_baseline_status(self.client_id, now=now)

        self.assertEqual(7, len(daily))
        self.assertFalse(status["available"])
        self.assertEqual("warmup", status["reason"])
        self.assertEqual(1, status["warmup_remaining"])

    def test_client_baselines_are_isolated(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        other_client = "client_other"
        for day_offset in range(1, 9):
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

    def _insert_daily_counts(self, counts: list[int]) -> datetime:
        """Insert one partial start date followed by completed usable days."""
        start = datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc)
        self._insert(start, start)
        for offset, count in enumerate(counts, start=1):
            occurred = start + timedelta(days=offset)
            for _ in range(count):
                self._insert(occurred, occurred)
        return start + timedelta(days=len(counts) + 1)

    def test_week_one_reports_only_baseline_formation_progress(self) -> None:
        now = self._insert_daily_counts([4, 5, 6, 5, 4])
        status = self.analytics.weekly_finding_status(self.client_id, now=now)
        self.assertEqual("baseline_formation", status["status"])
        self.assertEqual(1, status["active_week"])
        self.assertEqual(5, status["completed_days_in_week"])
        self.assertIsNone(status["latest_completed_period"])

    def test_week_two_stays_calculating_until_seven_usable_days(self) -> None:
        now = self._insert_daily_counts([4, 5, 6, 5, 4, 6, 5, 8, 9, 10])
        status = self.analytics.weekly_finding_status(self.client_id, now=now)
        self.assertEqual("calculating", status["status"])
        self.assertEqual(2, status["active_week"])
        self.assertEqual(3, status["completed_days_in_week"])
        self.assertIsNotNone(status["week_reference_snapshot"])
        self.assertIsNone(status["latest_completed_period"])

    def test_completed_week_uses_fixed_snapshot_and_temporary_weekly_ewma(self) -> None:
        counts = [4, 5, 6, 5, 4, 6, 5, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3]
        now = self._insert_daily_counts(counts)
        timeline = self.analytics.personal_ewma_timeline(self.client_id, now=now)
        status = self.analytics.weekly_finding_status(
            self.client_id, now=now, _timeline=timeline
        )
        latest = status["latest_completed_period"]

        self.assertEqual(3, status["active_week"])
        self.assertEqual(3, status["completed_days_in_week"])
        self.assertEqual(2, latest["week"])
        self.assertEqual(4.85, latest["week_reference_snapshot"])
        self.assertEqual(8.95, latest["weekly_level"])
        self.assertEqual(84.5, latest["change_percent"])
        self.assertNotEqual(
            latest["week_reference_snapshot"], timeline["current_baseline"]
        )

    def test_week_change_is_unavailable_for_zero_reference(self) -> None:
        days = []
        for index, count in enumerate([0] * 7 + [1] * 7):
            days.append(
                {
                    "date": f"2026-07-{index + 2:02d}",
                    "count": count,
                    "complete": True,
                    "usable": True,
                    "baseline_before": 0.0 if index == 7 else None,
                }
            )
        timeline = {
            "client_id": self.client_id,
            "start_date": "2026-07-01",
            "today": "2026-07-16",
            "completed_usable_days": 14,
            "warmup_remaining": 0,
            "current_baseline": 0.79,
            "days": days,
        }
        status = self.analytics.weekly_finding_status(
            self.client_id, _timeline=timeline
        )
        latest = status["latest_completed_period"]
        self.assertFalse(latest["change_available"])
        self.assertIsNone(latest["change_percent"])

    def test_day_night_streams_cross_midnight_and_hide_active_period(self) -> None:
        # 12:00 UTC is 19:00 local, so the current Night period is active while
        # the current Day period is already complete.
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        for offset in range(10, -1, -1):
            local_day = date(2026, 8, 20) - timedelta(days=offset)
            day_time = datetime.combine(
                local_day, datetime.min.time(), timezone(timedelta(hours=7))
            ).replace(hour=7)
            night_time = day_time.replace(hour=19)
            for _ in range(2 if offset else 5):
                self._insert(day_time, day_time)
            self._insert(night_time, night_time)
            # 02:00 belongs to the Night period that started the prior date.
            if offset:
                early = (day_time + timedelta(days=1)).replace(hour=2)
                self._insert(early, early)

        status = self.analytics.day_night_ewma_status(self.client_id, now=now)
        self.assertIn(status["day"]["trend"], {"increasing", "stable"})
        self.assertEqual("active_period", status["night"]["reason"])
        self.assertIsNone(status["night"]["trend"])


class DemoDataTest(unittest.TestCase):
    def test_demo_seed_exercises_dashboard_states_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            dao = Dao(str(Path(tempdir) / "demo.db"))
            dao.init_db(str(SCHEMA))
            try:
                now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
                first = seed_demo_data(dao, now=now)
                analytics = Analytics(dao)
                completed = analytics.get_client_stats(COMPLETED_CLIENT, now=now)
                week2 = analytics.get_client_stats(WEEK2_CLIENT, now=now)
                warmup = analytics.get_client_stats(WARMUP_CLIENT, now=now)

                self.assertTrue(first["created"])
                self.assertEqual(3, len(first["patients"]))
                self.assertEqual(18, completed["today_count"])
                self.assertTrue(completed["baseline"]["available"])
                self.assertEqual(3, completed["weekly_finding"]["active_week"])
                self.assertIsNotNone(
                    completed["weekly_finding"]["latest_completed_period"]
                )
                self.assertEqual(2, week2["weekly_finding"]["active_week"])
                self.assertEqual(
                    3, week2["weekly_finding"]["completed_days_in_week"]
                )
                self.assertGreater(completed["day_night_24h"]["day"], 0)
                self.assertGreater(completed["day_night_24h"]["night"], 0)
                self.assertTrue(all(completed["by_type_24h"].values()))
                self.assertTrue(completed["per_30_minute"])
                self.assertEqual(4, warmup["today_count"])
                self.assertFalse(warmup["baseline"]["available"])
                self.assertEqual(3, warmup["baseline"]["warmup_remaining"])
                self.assertEqual(4, warmup["weekly_finding"]["completed_days_in_week"])

                devices = {item["name"]: item for item in dao.get_devices()}
                self.assertEqual("offline", devices["Demo Sensor 01"]["status"])
                self.assertEqual(26.8, devices["Demo Sensor 01"]["temperature_c"])
                self.assertEqual("online", devices["Demo Sensor 02"]["status"])
                self.assertEqual(27.7, devices["Demo Sensor 03"]["temperature_c"])

                replay = next(
                    event
                    for event in dao.get_events_by_occurrence(COMPLETED_CLIENT)
                    if event["event_ts"][:10] != event["received_ts"][:10]
                )
                self.assertNotEqual(replay["event_ts"][:10], replay["received_ts"][:10])

                duplicate = seed_demo_data(dao, now=now)
                self.assertFalse(duplicate["created"])
                self.assertEqual(
                    first["events"],
                    len(dao.get_events(COMPLETED_CLIENT))
                    + len(dao.get_events(WEEK2_CLIENT))
                    + len(dao.get_events(WARMUP_CLIENT)),
                )

                replaced = seed_demo_data(dao, now=now, replace=True)
                self.assertTrue(replaced["created"])
                self.assertEqual(
                    first["events"],
                    len(dao.get_events(COMPLETED_CLIENT))
                    + len(dao.get_events(WEEK2_CLIENT))
                    + len(dao.get_events(WARMUP_CLIENT)),
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
        from ble_central import BleCentral

        class UnavailableNcp:
            @staticmethod
            def open() -> None:
                raise ConnectorException("serial port is unavailable")

        central = BleCentral.__new__(BleCentral)
        central.lib = UnavailableNcp()
        central.args = SimpleNamespace(serial_port="/dev/ttyACM0")

        with self.assertRaisesRegex(
            SystemExit, "Cannot open BGM220 NCP at /dev/ttyACM0"
        ):
            central.run()

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
