"""Generate isolated historical and live data for BreathSense dashboard testing."""

from __future__ import annotations

import argparse
import os
import random
import signal
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .analytics import DISPLAY_TIMEZONE
from .dao import Dao

SIM_PREFIX = "dashboard-sim-"
LEGACY_DEMO_MESSAGE_PREFIX = "demo-dashboard-"
LEGACY_DEMO_CLIENT_IDS = (
    "demo_patient_above_baseline",
    "demo_patient_warmup",
    "demo_patient_week2_incomplete",
    "demo_patient_week3",
)
LEGACY_DEMO_DEVICE_IDS = (
    "02:00:00:00:00:01",
    "02:00:00:00:00:02",
    "02:00:00:00:00:03",
)


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    type_weights: tuple[float, float, float]
    live_rate_per_hour: float

    @property
    def client_id(self) -> str:
        return f"{SIM_PREFIX}{self.key}"

    @property
    def device_id(self) -> str:
        return f"{SIM_PREFIX}device-{self.key}"


PROFILES = (
    Profile("stable", "Stable", (0.48, 0.40, 0.12), 2.0),
    Profile("needs-review", "Needs review", (0.50, 0.37, 0.13), 4.0),
    Profile("worsening", "Worsening", (0.55, 0.33, 0.12), 7.0),
    Profile("treatment-improving", "Treatment improving", (0.35, 0.55, 0.10), 1.8),
    Profile("warmup", "Warmup", (0.45, 0.38, 0.17), 2.5),
    Profile("irregular-missing", "Irregular / missing", (0.42, 0.36, 0.22), 1.0),
)

_HOUR_WEIGHTS = (
    0.15, 0.12, 0.08, 0.07, 0.08, 0.14,
    0.65, 0.95, 1.15, 1.10, 0.95, 0.80,
    0.75, 0.72, 0.82, 0.92, 1.05, 1.18,
    1.22, 1.15, 0.95, 0.70, 0.42, 0.25,
)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sim_exists(dao: Dao) -> bool:
    with dao._lock:
        row = dao._get_conn().execute(
            "SELECT COUNT(*) FROM cough_events WHERE message_id LIKE ?",
            (f"{SIM_PREFIX}%",),
        ).fetchone()
    return bool(row and row[0])


def cleanup_simulated_data(dao: Dao) -> None:
    """Delete current simulator rows plus rows from the retired demo generator."""
    sim_pattern = f"{SIM_PREFIX}%"
    legacy_pattern = f"{LEGACY_DEMO_MESSAGE_PREFIX}%"
    client_slots = ", ".join("?" for _ in LEGACY_DEMO_CLIENT_IDS)
    device_slots = ", ".join("?" for _ in LEGACY_DEMO_DEVICE_IDS)
    owned_row_where = (
        f"message_id LIKE ? OR message_id LIKE ? OR client_id IN ({client_slots}) "
        f"OR device_id IN ({device_slots})"
    )
    owned_row_params = (
        sim_pattern,
        legacy_pattern,
        *LEGACY_DEMO_CLIENT_IDS,
        *LEGACY_DEMO_DEVICE_IDS,
    )
    with dao._lock:
        connection = dao._get_conn()
        connection.execute(
            f"DELETE FROM cough_events WHERE {owned_row_where}", owned_row_params
        )
        connection.execute(
            f"DELETE FROM environment_readings WHERE {owned_row_where}",
            owned_row_params,
        )
        connection.execute(
            f"DELETE FROM client_settings WHERE client_id LIKE ? "
            f"OR client_id IN ({client_slots})",
            (sim_pattern, *LEGACY_DEMO_CLIENT_IDS),
        )
        connection.execute(
            f"DELETE FROM devices WHERE device_id LIKE ? "
            f"OR device_id IN ({device_slots}) OR client_id IN ({client_slots})",
            (sim_pattern, *LEGACY_DEMO_DEVICE_IDS, *LEGACY_DEMO_CLIENT_IDS),
        )
        connection.commit()


def _daily_targets(profile: Profile, local_today: date, rng: random.Random) -> list[tuple[date, int]]:
    if profile.key == "warmup":
        offsets = range(4, -1, -1)
        return [(local_today - timedelta(days=offset), rng.randint(4, 8)) for offset in offsets]
    if profile.key == "irregular-missing":
        offsets = (25, 24, 21, 18, 17, 12, 8, 7, 3, 1, 0)
        return [
            (
                local_today - timedelta(days=offset),
                1 if offset == 0 else 2 if offset == 1 else rng.randint(2, 12),
            )
            for offset in offsets
        ]

    schedule: list[tuple[date, int]] = []
    for offset in range(28, -1, -1):
        day = local_today - timedelta(days=offset)
        if profile.key == "stable":
            if offset == 0:
                count = 2
            elif offset == 1:
                count = 8
            else:
                count = max(9, min(15, int(round(rng.gauss(12, 2)))))
            schedule.append((day, count))
            continue
        if profile.key == "needs-review":
            if offset == 0:
                count = 30
            elif offset == 1:
                continue
            else:
                count = max(18, min(22, int(round(rng.gauss(20, 1)))))
            schedule.append((day, count))
            continue
        elif profile.key == "worsening":
            mean = 18 if offset <= 2 else 6
        else:
            age = 28 - offset
            mean = 16 if age < 7 else 12 if age < 14 else 8 if age < 21 else 5
            if offset == 1:
                mean = 3
            elif offset == 0:
                mean = 1
        count = max(1, int(round(rng.gauss(mean, max(mean * 0.18, 1.0)))))
        schedule.append((day, count))
    return schedule


def _event_times(day: date, count: int, local_now: datetime, rng: random.Random) -> list[datetime]:
    """Create irregular circadian timestamps with occasional short bursts."""
    end = (
        local_now
        if day == local_now.date()
        else datetime.combine(day, time.max, tzinfo=DISPLAY_TIMEZONE)
    )
    values: list[datetime] = []
    while len(values) < count:
        hour = rng.choices(range(24), weights=_HOUR_WEIGHTS, k=1)[0]
        anchor = datetime.combine(
            day,
            time(hour, rng.randrange(60), rng.randrange(60)),
            tzinfo=DISPLAY_TIMEZONE,
        )
        if anchor > end:
            continue
        values.append(anchor)
        if len(values) < count and rng.random() < 0.24:
            for _ in range(rng.randint(1, 2)):
                if len(values) >= count:
                    break
                burst = anchor + timedelta(seconds=rng.randint(45, 12 * 60))
                if burst <= end and burst.date() == day:
                    values.append(burst)
    return sorted(values[:count])


def _event_counter(dao: Dao, device_id: str) -> int:
    with dao._lock:
        row = dao._get_conn().execute(
            "SELECT MAX(event_counter) FROM cough_events WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    return int(row[0] or 0)


def _insert_event(
    dao: Dao,
    profile: Profile,
    occurred: datetime,
    counter: int,
    serial: str,
    rng: random.Random,
    received: datetime | None = None,
) -> bool:
    cough_type = rng.choices(
        ("dry", "wet", "unknown"), weights=profile.type_weights, k=1
    )[0]
    prolonged = rng.random() < 0.045
    duration_s = rng.randint(5, 12) if prolonged else rng.randint(1, 4)
    flags = (
        0x01
        | (0x02 if cough_type != "unknown" else 0)
        | (0x04 if prolonged else 0)
        | ((duration_s & 0x1F) << 3)
    )
    received_at = received or occurred + timedelta(seconds=rng.uniform(0.2, 3.0))
    now_utc = datetime.now(timezone.utc)
    if received_at.astimezone(timezone.utc) > now_utc and occurred.astimezone(timezone.utc) <= now_utc:
        received_at = now_utc
    return dao.insert_event(
        {
            "message_id": f"{SIM_PREFIX}{profile.key}-{serial}",
            "session_id": f"{SIM_PREFIX}session",
            "device_id": profile.device_id,
            "client_id": profile.client_id,
            "cough_type": cough_type,
            "event_ts": _utc_iso(occurred),
            "received_ts": _utc_iso(received_at),
            "event_counter": counter & 0xFFFF,
            "node_event_timestamp": int(occurred.astimezone(timezone.utc).timestamp()),
            "timestamp_source": "node_unix_seconds",
            "flags": flags,
            "timestamp_valid": True,
            "stage2_valid": cough_type != "unknown",
            "prolonged": prolonged,
            "duration_s": duration_s,
            "payload_hex": None,
        }
    ) is not None


def _upsert_profile_device(dao: Dao, profile: Profile, now_utc: datetime) -> None:
    dao.upsert_device(
        profile.device_id,
        name=f"Dashboard Simulator — {profile.label}",
        address_type=0,
        client_id=profile.client_id,
        status="online",
        last_seen=_utc_iso(now_utc),
    )


def _insert_environment(dao: Dao, profile: Profile, now_utc: datetime, rng: random.Random) -> None:
    temperature = round(rng.uniform(25.8, 29.1), 2)
    humidity = round(rng.uniform(55.0, 68.0), 2)
    dao.insert_environment(
        {
            "message_id": f"{SIM_PREFIX}environment-{profile.key}",
            "session_id": f"{SIM_PREFIX}session",
            "device_id": profile.device_id,
            "client_id": profile.client_id,
            "event_ts": _utc_iso(now_utc),
            "received_ts": _utc_iso(now_utc),
            "temperature_c": temperature,
            "humidity_percent": humidity,
            "temperature_x100": round(temperature * 100),
            "humidity_x100": round(humidity * 100),
            "payload_hex": None,
        }
    )


def simulate_history(
    dao: Dao,
    *,
    now: datetime | None = None,
    seed: int = 20260823,
    replace: bool = False,
) -> dict:
    """Backfill all scenarios once; the same seed gives the same shape."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    if replace:
        cleanup_simulated_data(dao)
    elif _sim_exists(dao):
        return {"created": False, "reason": "simulated_data_already_exists"}

    local_now = now_utc.astimezone(DISPLAY_TIMEZONE)
    rng = random.Random(seed)
    total = 0
    patient_counts: dict[str, int] = {}
    for profile in PROFILES:
        _upsert_profile_device(dao, profile, now_utc)
        _insert_environment(dao, profile, now_utc, rng)
        counter = 0
        created = 0
        for day, target in _daily_targets(profile, local_now.date(), rng):
            for index, occurred in enumerate(_event_times(day, target, local_now, rng)):
                counter = (counter + 1) & 0xFFFF
                serial = f"history-{day.isoformat()}-{index:04d}"
                created += int(
                    _insert_event(dao, profile, occurred, counter, serial, rng)
                )
        patient_counts[profile.client_id] = created
        total += created
    return {
        "created": True,
        "seed": seed,
        "events": total,
        "patients": patient_counts,
    }


def insert_live_event(
    dao: Dao,
    profile: Profile,
    rng: random.Random,
    serial: int,
    now: datetime | None = None,
) -> bool:
    """Insert one current event through the same SQLite data contract."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    _upsert_profile_device(dao, profile, now_utc)
    counter = (_event_counter(dao, profile.device_id) + 1) & 0xFFFF
    return _insert_event(
        dao,
        profile,
        now_utc.astimezone(DISPLAY_TIMEZONE),
        counter,
        f"live-{serial:010d}",
        rng,
        received=now_utc,
    )


def run_live(dao: Dao, *, seed: int, time_scale: float = 1.0) -> None:
    """Keep inserting stochastic live events until interrupted."""
    if time_scale <= 0:
        raise ValueError("time_scale must be positive")
    rng = random.Random(seed + 1)
    serial = 0
    next_due = {
        profile.key: clock.monotonic()
        + rng.expovariate(profile.live_rate_per_hour / 3600.0) / time_scale
        for profile in PROFILES
    }
    mode = "real time" if time_scale == 1.0 else f"accelerated test mode ({time_scale:g}x)"
    print(f"Live simulator running in {mode}; press Ctrl+C to stop.", flush=True)
    while True:
        profile = min(PROFILES, key=lambda item: next_due[item.key])
        delay = max(next_due[profile.key] - clock.monotonic(), 0.0)
        if delay:
            clock.sleep(min(delay, 1.0))
            continue
        serial += 1
        insert_live_event(dao, profile, rng, serial)
        print(f"live event: {profile.client_id}", flush=True)
        next_due[profile.key] = clock.monotonic() + (
            rng.expovariate(profile.live_rate_per_hour / 3600.0) / time_scale
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("GATEWAY_DB_PATH", "cough_monitor.db"),
        help="SQLite database path",
    )
    parser.add_argument("--replace", action="store_true", help="Replace simulator-owned rows")
    parser.add_argument("--seed", type=int, default=20260823, help="Reproducible random seed")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--history-only", action="store_true", help="Backfill and exit")
    mode.add_argument("--live-only", action="store_true", help="Skip historical backfill")
    parser.add_argument(
        "--time-scale",
        type=float,
        default=1.0,
        help="Accelerate live delays without creating future timestamps",
    )
    args = parser.parse_args()
    if args.time_scale <= 0:
        parser.error("--time-scale must be positive")

    dao = Dao(args.db)
    dao.init_db(str(Path(__file__).with_name("schema.sql")))
    try:
        if not args.live_only:
            print(
                simulate_history(dao, seed=args.seed, replace=args.replace),
                flush=True,
            )
        elif args.replace:
            cleanup_simulated_data(dao)
        if args.history_only:
            return
        for profile in PROFILES:
            _upsert_profile_device(dao, profile, datetime.now(timezone.utc))
        run_live(dao, seed=args.seed, time_scale=args.time_scale)
    except KeyboardInterrupt:
        print("Simulator stopped.")
    finally:
        dao.close()


def raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: raise_keyboard_interrupt())
    main()
