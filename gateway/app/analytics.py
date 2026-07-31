"""Per-client aggregates and trend queries."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dao import Dao

try:
    DISPLAY_TIMEZONE = ZoneInfo(os.environ.get("GATEWAY_TIMEZONE", "Asia/Ho_Chi_Minh"))
except ZoneInfoNotFoundError:
    DISPLAY_TIMEZONE = ZoneInfo("UTC")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TIMEZONE)
    except ValueError:
        return None


class Analytics:
    """Computes cough counts, rates, and local-time trends per client."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def get_client_stats(self, client_id: str) -> dict:
        now = datetime.now(timezone.utc)
        all_events = self.dao.get_events(client_id=client_id)
        events_24h = self.dao.get_events(
            client_id=client_id,
            start_time=(now - timedelta(hours=24)).isoformat(),
            end_time=now.isoformat(),
        )
        events_7d = self.dao.get_events(
            client_id=client_id,
            start_time=(now - timedelta(days=7)).isoformat(),
            end_time=now.isoformat(),
        )

        by_type = {"dry": 0, "wet": 0, "unknown": 0}
        hourly_map: defaultdict[str, int] = defaultdict(int)
        daily_map: defaultdict[str, int] = defaultdict(int)
        for event in all_events:
            cough_type = event.get("cough_type", "unknown")
            by_type[cough_type if cough_type in by_type else "unknown"] += 1
        for event in events_24h:
            ts = _parse_ts(event.get("event_ts") or event.get("received_ts"))
            if ts:
                hourly_map[ts.strftime("%Y-%m-%dT%H:00:00%z")] += 1
        for event in events_7d:
            ts = _parse_ts(event.get("event_ts") or event.get("received_ts"))
            if ts:
                daily_map[ts.strftime("%Y-%m-%d")] += 1

        return {
            "total": len(all_events),
            "by_type": by_type,
            "per_hour": [{"ts": key, "count": value} for key, value in sorted(hourly_map.items())],
            "per_day": [{"date": key, "count": value} for key, value in sorted(daily_map.items())],
        }

    def hourly_counts(self, client_id: str, days: int = 7) -> list[dict]:
        now = datetime.now(timezone.utc)
        events = self.dao.get_events(
            client_id=client_id,
            start_time=(now - timedelta(days=days)).isoformat(),
            end_time=now.isoformat(),
        )
        hourly_map: defaultdict[str, int] = defaultdict(int)
        for event in events:
            ts = _parse_ts(event.get("event_ts") or event.get("received_ts"))
            if ts:
                hourly_map[ts.strftime("%Y-%m-%dT%H:00:00%z")] += 1
        return [{"ts": key, "count": value} for key, value in sorted(hourly_map.items())]

    def rate(self, client_id: str, window_h: int = 24) -> float:
        if window_h <= 0:
            return 0.0
        now = datetime.now(timezone.utc)
        events = self.dao.get_events(
            client_id=client_id,
            start_time=(now - timedelta(hours=window_h)).isoformat(),
            end_time=now.isoformat(),
        )
        return len(events) / window_h


    def daily_cough_counts(
        self,
        client_id: str,
        days: int = 30,
    ) -> list[dict]:
        """Return observed cough counts grouped by local calendar day.

        Only days containing at least one stored cough event are returned.
        A missing day is not automatically treated as zero because the
        gateway may have been offline or the patient may not have been
        monitored during that day.
        """
        if days <= 0:
            return []

        now = datetime.now(timezone.utc)
        events = self.dao.get_events(
            client_id=client_id,
            # Lấy dư một ngày để tránh cắt mất phần đầu của ngày biên.
            start_time=(now - timedelta(days=days + 1)).isoformat(),
            end_time=now.isoformat(),
        )

        daily_map: defaultdict[str, int] = defaultdict(int)

        for event in events:
            ts = _parse_ts(
                event.get("event_ts") or event.get("received_ts")
            )
            if ts is not None:
                daily_map[ts.strftime("%Y-%m-%d")] += 1

        return [
            {"date": key, "count": value}
            for key, value in sorted(daily_map.items())
        ]

    def ewma_baseline_status(
        self,
        client_id: str,
        alpha: float = 0.2,
        threshold_pct: float = 0.4,
        min_buffer: float = 5.0,
        history_days: int = 30,
    ) -> dict:
        """Compare today's cough count with an EWMA baseline.

        The first completed observed day initializes the baseline.
        Abnormal historical days are evaluated but are not allowed to
        increase the baseline.
        """
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in the range (0, 1]")

        if threshold_pct < 0.0:
            raise ValueError("threshold_pct must be non-negative")

        if min_buffer < 0.0:
            raise ValueError("min_buffer must be non-negative")

        daily_counts = self.daily_cough_counts(
            client_id=client_id,
            days=history_days,
        )

        today = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d")

        today_count = 0
        completed_days: list[dict] = []

        for item in daily_counts:
            if item["date"] == today:
                today_count = int(item["count"])
            elif item["date"] < today:
                completed_days.append(item)

        if not completed_days:
            return {
                "available": False,
                "client_id": client_id,
                "today": today,
                "today_count": today_count,
                "baseline": None,
                "max_allowed": None,
                "abnormal": False,
                "observed_history_days": 0,
                "reason": "no_completed_history",
            }

        baseline = float(completed_days[0]["count"])

        for item in completed_days[1:]:
            count = int(item["count"])
            allowed_increase = max(
                baseline * threshold_pct,
                min_buffer,
            )
            max_allowed = baseline + allowed_increase
            historical_abnormal = count > max_allowed

            # Không để ngày bất thường kéo mức nền tăng lên.
            if not historical_abnormal:
                baseline = (
                    alpha * count
                    + (1.0 - alpha) * baseline
                )

        allowed_increase = max(
            baseline * threshold_pct,
            min_buffer,
        )
        max_allowed = baseline + allowed_increase
        abnormal = today_count > max_allowed

        return {
            "available": True,
            "client_id": client_id,
            "today": today,
            "today_count": today_count,
            "baseline": round(baseline, 2),
            "max_allowed": round(max_allowed, 2),
            "abnormal": abnormal,
            "observed_history_days": len(completed_days),
            "alpha": alpha,
            "threshold_pct": threshold_pct,
            "min_buffer": min_buffer,
        }

    def rate_previous(self, client_id: str, window_h: int = 24) -> float:
        if window_h <= 0:
            return 0.0
        now = datetime.now(timezone.utc)
        end_time = now - timedelta(hours=window_h)
        events = self.dao.get_events(
            client_id=client_id,
            start_time=(end_time - timedelta(hours=window_h)).isoformat(),
            end_time=end_time.isoformat(),
        )
        return len(events) / window_h
