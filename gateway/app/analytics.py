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
