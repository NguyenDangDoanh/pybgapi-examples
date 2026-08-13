"""Create isolated BreathSense demo patients for dashboard verification."""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .analytics import DISPLAY_TIMEZONE
from .dao import Dao

DEMO_PREFIX = "demo-dashboard-"
ABOVE_CLIENT = "demo_patient_above_baseline"
WARMUP_CLIENT = "demo_patient_warmup"
ABOVE_DEVICE = "02:00:00:00:00:01"
WARMUP_DEVICE = "02:00:00:00:00:02"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _local_datetime(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour=hour, minute=minute), DISPLAY_TIMEZONE)


def _today_times(local_now: datetime, count: int) -> list[datetime]:
    """Distribute demo bouts before now without creating future timestamps."""
    midnight = datetime.combine(local_now.date(), time.min, DISPLAY_TIMEZONE)
    elapsed = max((local_now - midnight).total_seconds(), count + 1)
    return [
        midnight + timedelta(seconds=elapsed * (index + 1) / (count + 1))
        for index in range(count)
    ]


def _historical_times(day: date, count: int) -> list[datetime]:
    hours = (2, 7, 9, 11, 14, 17, 19, 21, 23)
    return [
        _local_datetime(day, hours[index % len(hours)], (index * 7) % 60)
        for index in range(count)
    ]


def _delete_existing_demo(dao: Dao) -> None:
    """Delete only rows owned by this demo generator."""
    with dao._lock:
        connection = dao._get_conn()
        pattern = f"{DEMO_PREFIX}%"
        connection.execute(
            "DELETE FROM cough_events WHERE message_id LIKE ?", (pattern,)
        )
        connection.execute(
            "DELETE FROM environment_readings WHERE message_id LIKE ?", (pattern,)
        )
        connection.commit()


def _demo_exists(dao: Dao) -> bool:
    with dao._lock:
        row = dao._get_conn().execute(
            "SELECT COUNT(*) FROM cough_events WHERE message_id LIKE ?",
            (f"{DEMO_PREFIX}%",),
        ).fetchone()
    return bool(row and row[0])


def _insert_bouts(
    dao: Dao,
    client_id: str,
    device_id: str,
    schedule: list[tuple[date, int]],
    local_now: datetime,
) -> int:
    counter = 0
    total = 0
    cough_types = ("dry", "wet", "unknown", "dry", "wet")
    for day, count in schedule:
        occurred_times = (
            _today_times(local_now, count)
            if day == local_now.date()
            else _historical_times(day, count)
        )
        for index, occurred in enumerate(occurred_times):
            counter = (counter + 1) & 0xFFFF
            cough_type = cough_types[(total + index) % len(cough_types)]
            prolonged = (total + index) % 6 == 0
            duration_s = 6 if prolonged else 2 + ((total + index) % 2)
            flags = (
                0x01
                | (0x02 if cough_type != "unknown" else 0)
                | (0x04 if prolonged else 0)
                | ((duration_s & 0x1F) << 3)
            )
            received = occurred + timedelta(seconds=2)
            # One delayed row exercises event_ts vs received_ts in Live Feed.
            if day == local_now.date() and index == 0:
                received = local_now - timedelta(seconds=5)
            event_ts = _utc_iso(occurred)
            dao.insert_event(
                {
                    "message_id": (
                        f"{DEMO_PREFIX}{client_id}-{day.isoformat()}-{index:03d}"
                    ),
                    "session_id": "demo-session",
                    "device_id": device_id,
                    "client_id": client_id,
                    "cough_type": cough_type,
                    "event_ts": event_ts,
                    "received_ts": _utc_iso(received),
                    "event_counter": counter,
                    "node_event_timestamp": int(
                        occurred.astimezone(timezone.utc).timestamp()
                    ),
                    "timestamp_source": "node_unix_seconds",
                    "flags": flags,
                    "timestamp_valid": True,
                    "stage2_valid": cough_type != "unknown",
                    "prolonged": prolonged,
                    "duration_s": duration_s,
                    "payload_hex": None,
                }
            )
            total += 1
    return total


def seed_demo_data(
    dao: Dao, now: datetime | None = None, replace: bool = False
) -> dict:
    """Seed two demo patients; return a compact result summary."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(DISPLAY_TIMEZONE)

    if _demo_exists(dao):
        if not replace:
            return {"created": False, "reason": "demo_data_already_exists"}
        _delete_existing_demo(dao)

    above_last_seen = _utc_iso(now_utc)
    warmup_last_seen = _utc_iso(now_utc - timedelta(minutes=35))
    dao.upsert_device(
        ABOVE_DEVICE,
        name="Demo Sensor 01",
        address_type=0,
        client_id=ABOVE_CLIENT,
        status="online",
        last_seen=above_last_seen,
    )
    dao.upsert_device(
        WARMUP_DEVICE,
        name="Demo Sensor 02",
        address_type=0,
        client_id=WARMUP_CLIENT,
        status="offline",
        last_seen=warmup_last_seen,
    )

    above_history = [8, 9, 10, 9, 11, 10, 10]
    above_schedule = [
        (local_now.date() - timedelta(days=7 - index), count)
        for index, count in enumerate(above_history)
    ]
    above_schedule.append((local_now.date(), 22))

    warmup_schedule = [
        (local_now.date() - timedelta(days=3), 5),
        (local_now.date() - timedelta(days=2), 7),
        (local_now.date() - timedelta(days=1), 6),
        (local_now.date(), 4),
    ]

    above_count = _insert_bouts(
        dao, ABOVE_CLIENT, ABOVE_DEVICE, above_schedule, local_now
    )
    warmup_count = _insert_bouts(
        dao, WARMUP_CLIENT, WARMUP_DEVICE, warmup_schedule, local_now
    )

    dao.insert_environment(
        {
            "message_id": f"{DEMO_PREFIX}environment-01",
            "session_id": "demo-session",
            "device_id": ABOVE_DEVICE,
            "client_id": ABOVE_CLIENT,
            "event_ts": above_last_seen,
            "received_ts": above_last_seen,
            "temperature_c": 27.4,
            "humidity_percent": 63.2,
            "temperature_x100": 2740,
            "humidity_x100": 6320,
            "payload_hex": None,
        }
    )
    dao.insert_environment(
        {
            "message_id": f"{DEMO_PREFIX}environment-02",
            "session_id": "demo-session",
            "device_id": WARMUP_DEVICE,
            "client_id": WARMUP_CLIENT,
            "event_ts": warmup_last_seen,
            "received_ts": warmup_last_seen,
            "temperature_c": 26.8,
            "humidity_percent": 61.5,
            "temperature_x100": 2680,
            "humidity_x100": 6150,
            "payload_hex": None,
        }
    )

    return {
        "created": True,
        "patients": [ABOVE_CLIENT, WARMUP_CLIENT],
        "events": above_count + warmup_count,
        "above_baseline_events": above_count,
        "warmup_events": warmup_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("GATEWAY_DB_PATH", "cough_monitor.db"),
        help="SQLite database path (default: GATEWAY_DB_PATH or cough_monitor.db)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace only rows created by this demo generator",
    )
    args = parser.parse_args()

    dao = Dao(args.db)
    schema_path = Path(__file__).with_name("schema.sql")
    dao.init_db(str(schema_path))
    try:
        result = seed_demo_data(dao, replace=args.replace)
    finally:
        dao.close()
    print(result)


if __name__ == "__main__":
    main()
