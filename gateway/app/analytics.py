"""Per-client aggregates and trend queries (pure reads, no state).

All methods read from the DAO — no writes, no caching.  stats_for_client
returns exactly the shape expected by GET /api/clients/{id}/stats and the
dashboard callbacks.

See design/gateway_app.md.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .dao import Dao

logger = logging.getLogger(__name__)


class Analytics:
    """Computes cough counts, rates, and time-bucketed trends per client."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def get_client_stats(self, client_id: str) -> dict:
        """Full stats bundle for one client.

        Returns:
            {"total": int, "by_type": {"dry": int, "wet": int, "unknown": int},
             "per_hour": [{"ts": str, "count": int}, ...],
             "per_day": [{"date": str, "count": int}, ...]}
        """
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
        hourly_map = defaultdict(int)
        daily_map = defaultdict(int)

        for event in all_events:
            cough_type = event.get("cough_type", "unknown")
            if cough_type in by_type:
                by_type[cough_type] += 1
            else:
                by_type["unknown"] += 1

        for event in events_24h:
            ts = event.get("event_ts") or event.get("received_ts", "")
            if len(ts) >= 13:
                hourly_map[ts[:13]] += 1

        for event in events_7d:
            ts = event.get("event_ts") or event.get("received_ts", "")
            if len(ts) >= 10:
                daily_map[ts[:10]] += 1

        per_hour = [{"ts": key, "count": value} for key, value in sorted(hourly_map.items())]
        per_day = [{"date": key, "count": value} for key, value in sorted(daily_map.items())]

        return {
            "total": len(all_events),
            "by_type": by_type,
            "per_hour": per_hour,
            "per_day": per_day,
        }

    def hourly_counts(self, client_id: str, days: int = 7) -> list[dict]:
        """Per-hour cough counts over the last `days` days."""
        now = datetime.now(timezone.utc)
        start_time = (now - timedelta(days=days)).isoformat()

        events = self.dao.get_events(
            client_id=client_id,
            start_time=start_time,
            end_time=now.isoformat(),
        )
        hourly_map = defaultdict(int)

        for event in events:
            ts = event.get("received_ts", "")
            if len(ts) >= 13:
                hourly_map[ts[:13]] += 1

        return [{"ts": key, "count": value} for key, value in sorted(hourly_map.items())]

    def rate(self, client_id: str, window_h: int = 24) -> float:
        """Cough events per hour over the most recent `window_h` hours."""
        if window_h <= 0:
            return 0.0

        now = datetime.now(timezone.utc)
        start_time = (now - timedelta(hours=window_h)).isoformat()

        events = self.dao.get_events(
            client_id=client_id,
            start_time=start_time,
            end_time=now.isoformat(),
        )
        return len(events) / window_h

    def rate_previous(self, client_id: str, window_h: int = 24) -> float:
        """Cough rate for the preceding `window_h`-hour window."""
        if window_h <= 0:
            return 0.0

        now = datetime.now(timezone.utc)
        end_time = now - timedelta(hours=window_h)
        start_time = end_time - timedelta(hours=window_h)

        events = self.dao.get_events(
            client_id=client_id,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
        )
        return len(events) / window_h
