"""Per-client cough-bout aggregates and personal statistical baseline."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dao import Dao

try:
    DISPLAY_TIMEZONE = ZoneInfo(os.environ.get("GATEWAY_TIMEZONE", "Asia/Ho_Chi_Minh"))
except ZoneInfoNotFoundError:
    DISPLAY_TIMEZONE = ZoneInfo("UTC")

DAY_START_HOUR = 6
NIGHT_START_HOUR = 18


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp and return it in the configured local timezone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TIMEZONE)
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _local_midnight_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=DISPLAY_TIMEZONE).astimezone(
        timezone.utc
    )


def _type_counts(events: list[dict]) -> dict[str, int]:
    counts = {"wet": 0, "dry": 0, "unknown": 0}
    for event in events:
        cough_type = str(event.get("cough_type") or "unknown").lower()
        counts[cough_type if cough_type in counts else "unknown"] += 1
    return counts


def _day_night_counts(events: list[dict]) -> dict[str, int]:
    counts = {"day": 0, "night": 0}
    for event in events:
        occurred = _parse_ts(event.get("event_ts"))
        if occurred is None:
            continue
        period = (
            "day"
            if DAY_START_HOUR <= occurred.hour < NIGHT_START_HOUR
            else "night"
        )
        counts[period] += 1
    return counts


def _ten_minute_key(occurred: datetime) -> str:
    """Return the local start timestamp of the event's 10-minute bucket."""
    bucket = occurred.replace(
        minute=(occurred.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    return bucket.strftime("%Y-%m-%dT%H:%M:%S%z")


class Analytics:
    """Compute occurrence-time bout trends independently for each client."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def get_client_stats(
        self, client_id: str, now: datetime | None = None
    ) -> dict:
        occurrence_events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            limit=1,
            descending=True,
        )
        last_event_ts = (
            occurrence_events[0].get("event_ts") if occurrence_events else None
        )
        transport_events = self.dao.get_events(client_id=client_id, limit=1)
        last_received_ts = (
            transport_events[0].get("received_ts") if transport_events else None
        )
        if now is None and last_event_ts:
            parsed_anchor = _parse_ts(last_event_ts)
            now_utc = (
                parsed_anchor.astimezone(timezone.utc)
                if parsed_anchor is not None
                else _as_utc(None)
            )
        else:
            now_utc = _as_utc(now)
        local_now = now_utc.astimezone(DISPLAY_TIMEZONE)
        start_24h = now_utc - timedelta(hours=24)
        start_7d = _local_midnight_utc(local_now.date() - timedelta(days=6))
        start_hour_history = now_utc - timedelta(days=7)

        events_24h = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_24h),
            end_time=_iso_utc(now_utc),
        )
        events_7d = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_7d),
            end_time=_iso_utc(now_utc),
        )
        events_hour_history = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_hour_history),
            end_time=_iso_utc(now_utc),
        )

        hourly_map: defaultdict[str, int] = defaultdict(int)
        ten_minute_map: defaultdict[str, int] = defaultdict(int)
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_map[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
                ten_minute_map[_ten_minute_key(occurred)] += 1

        daily_map: defaultdict[str, int] = defaultdict(int)
        for event in events_7d:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                daily_map[occurred.strftime("%Y-%m-%d")] += 1

        hourly_history_map: defaultdict[str, int] = defaultdict(int)
        ten_minute_history_map: defaultdict[str, int] = defaultdict(int)
        for event in events_hour_history:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_history_map[
                    occurred.strftime("%Y-%m-%dT%H:00:00%z")
                ] += 1
                ten_minute_history_map[_ten_minute_key(occurred)] += 1

        all_events = self.dao.get_events(client_id=client_id)
        baseline = self.ewma_baseline_status(client_id, now=now_utc)
        client_settings = self.dao.get_client_settings(client_id)
        treatment_response = self.treatment_response_status(
            client_id,
            treatment_start_date=client_settings.get("treatment_start_date"),
            now=now_utc,
        )

        by_type_24h = _type_counts(events_24h)
        by_type_7d = _type_counts(events_7d)
        day_night_24h = _day_night_counts(events_24h)
        day_night_7d = _day_night_counts(events_7d)

        return {
            # Legacy keys remain available to existing API consumers. They now
            # describe the dashboard's default 24-hour occurrence-time range.
            "total": len(all_events),
            "by_type": by_type_24h,
            "per_hour": [
                {"ts": key, "count": value}
                for key, value in sorted(hourly_map.items())
            ],
            "per_10_minute": [
                {"ts": key, "count": value}
                for key, value in sorted(ten_minute_map.items())
            ],
            "per_day": [
                {"date": key, "count": value}
                for key, value in sorted(daily_map.items())
            ],
            "per_hour_history": [
                {"ts": key, "count": value}
                for key, value in sorted(hourly_history_map.items())
            ],
            "per_10_minute_history": [
                {"ts": key, "count": value}
                for key, value in sorted(ten_minute_history_map.items())
            ],
            "last_24h_count": len(events_24h),
            "last_7d_count": len(events_7d),
            "today_count": baseline["today_count"],
            "by_type_24h": by_type_24h,
            "by_type_7d": by_type_7d,
            "day_night_24h": day_night_24h,
            "day_night_7d": day_night_7d,
            "baseline": baseline,
            "treatment_response": treatment_response,
            "last_event_ts": last_event_ts,
            # Transport receipt remains available to API consumers for audit
            # and reconnect diagnostics, but does not drive patient analytics.
            "last_received_ts": last_received_ts,
            "analysis_anchor_ts": _iso_utc(now_utc),
        }

    def treatment_response_status(
        self,
        client_id: str,
        treatment_start_date: str | None,
        warmup_days: int = 7,
        now: datetime | None = None,
    ) -> dict:
        """Compare the latest completed treatment day with prior full days.

        The first local calendar day containing data is conservatively treated
        as partial. Every later completed calendar day is included, including
        zero-bout days under the project's continuous-monitoring assumption.
        The current day is never compared with a baseline that contains itself;
        once completed, it joins the expanding baseline for the following day.
        """
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")

        base = {
            "client_id": client_id,
            "treatment_start_date": treatment_start_date,
            "warmup_days": warmup_days,
            "available": False,
            "baseline": None,
            "current": None,
            "current_date": None,
            "change_percent": None,
            "direction": None,
            "baseline_days": 0,
            "warmup_remaining": warmup_days,
        }
        if not treatment_start_date:
            return {**base, "reason": "treatment_not_set"}
        try:
            treatment_day = date.fromisoformat(str(treatment_start_date))
        except (TypeError, ValueError):
            return {**base, "reason": "invalid_treatment_date"}

        now_utc = _as_utc(now)
        completed_through = (
            now_utc.astimezone(DISPLAY_TIMEZONE).date() - timedelta(days=1)
        )
        events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            end_time=_iso_utc(now_utc),
        )
        dated_events: list[tuple[date, dict]] = []
        for event in events:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                dated_events.append((occurred.date(), event))
        if not dated_events:
            return {**base, "reason": "no_data"}

        # We cannot prove that monitoring covered the part of the first day
        # before the first event, so full-day accounting begins the next day.
        first_full_day = min(day for day, _event in dated_events) + timedelta(days=1)
        if first_full_day > completed_through:
            return {**base, "reason": "no_completed_day"}

        counts: defaultdict[date, int] = defaultdict(int)
        for day, _event in dated_events:
            if first_full_day <= day <= completed_through:
                counts[day] += 1

        completed_days: list[dict] = []
        cursor = first_full_day
        while cursor <= completed_through:
            completed_days.append({"date": cursor, "count": counts[cursor]})
            cursor += timedelta(days=1)

        treatment_days = [
            item for item in completed_days if item["date"] >= treatment_day
        ]
        if not treatment_days:
            return {
                **base,
                "completed_days": len(completed_days),
                "reason": "awaiting_completed_treatment_day",
            }

        current_day = treatment_days[-1]
        baseline_days = [
            item for item in completed_days if item["date"] < current_day["date"]
        ]
        remaining = max(warmup_days - len(baseline_days), 0)
        pending = {
            **base,
            "current": int(current_day["count"]),
            "current_date": current_day["date"].isoformat(),
            "baseline_days": len(baseline_days),
            "completed_days": len(completed_days),
            "warmup_remaining": remaining,
        }
        if remaining:
            return {**pending, "reason": "warmup"}

        cumulative_baseline = sum(
            int(item["count"]) for item in baseline_days
        ) / len(baseline_days)
        current_count = int(current_day["count"])
        change_percent = (
            ((current_count - cumulative_baseline) / cumulative_baseline) * 100.0
            if cumulative_baseline > 0
            else None
        )
        if current_count < cumulative_baseline:
            direction = "decreased"
        elif current_count > cumulative_baseline:
            direction = "increased"
        else:
            direction = "unchanged"

        return {
            **pending,
            "available": True,
            "baseline": round(cumulative_baseline, 2),
            "change_percent": (
                round(change_percent, 1) if change_percent is not None else None
            ),
            "direction": direction,
            "reason": None if change_percent is not None else "zero_baseline",
        }

    def hourly_counts(
        self, client_id: str, days: int = 7, now: datetime | None = None
    ) -> list[dict]:
        if days <= 0:
            return []
        now_utc = _as_utc(now)
        events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(now_utc - timedelta(days=days)),
            end_time=_iso_utc(now_utc),
        )
        hourly_map: defaultdict[str, int] = defaultdict(int)
        for event in events:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_map[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
        return [
            {"ts": key, "count": value} for key, value in sorted(hourly_map.items())
        ]

    def rate(
        self, client_id: str, window_h: int = 24, now: datetime | None = None
    ) -> float:
        if window_h <= 0:
            return 0.0
        now_utc = _as_utc(now)
        events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(now_utc - timedelta(hours=window_h)),
            end_time=_iso_utc(now_utc),
        )
        return len(events) / window_h

    def daily_cough_counts(
        self,
        client_id: str,
        days: int | None = None,
        now: datetime | None = None,
    ) -> list[dict]:
        """Return observed bout counts by local day; missing days are omitted."""
        if days is not None and days <= 0:
            return []

        now_utc = _as_utc(now)
        start_time = None
        if days is not None:
            local_now = now_utc.astimezone(DISPLAY_TIMEZONE)
            start_time = _iso_utc(
                _local_midnight_utc(local_now.date() - timedelta(days=days - 1))
            )

        events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=start_time,
            end_time=_iso_utc(now_utc),
        )
        daily_map: defaultdict[str, int] = defaultdict(int)
        for event in events:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                daily_map[occurred.strftime("%Y-%m-%d")] += 1
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
        warmup_days: int = 7,
        now: datetime | None = None,
    ) -> dict:
        """Compare today's observed bout count with the ongoing EWMA baseline."""
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in the range (0, 1]")
        if threshold_pct < 0.0:
            raise ValueError("threshold_pct must be non-negative")
        if min_buffer < 0.0:
            raise ValueError("min_buffer must be non-negative")
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")

        now_utc = _as_utc(now)
        today = now_utc.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d")
        daily_counts = self.daily_cough_counts(client_id=client_id, now=now_utc)

        today_count: int | None = None
        completed_days: list[dict] = []
        for item in daily_counts:
            if item["date"] == today:
                today_count = int(item["count"])
            elif item["date"] < today:
                completed_days.append(item)

        base = {
            "client_id": client_id,
            "today": today,
            "today_count": today_count,
            "current_available": today_count is not None,
            "observed_history_days": len(completed_days),
            "warmup_days": warmup_days,
            "warmup_remaining": max(warmup_days - len(completed_days), 0),
            "alpha": alpha,
            "threshold_pct": threshold_pct,
            "min_buffer": min_buffer,
        }

        if len(completed_days) < warmup_days:
            return {
                **base,
                "available": False,
                "baseline": None,
                "threshold": None,
                "max_allowed": None,
                "change_percent": None,
                "above_baseline": False,
                "abnormal": False,
                "reason": "warmup",
            }

        baseline = float(completed_days[0]["count"])
        for item in completed_days[1:]:
            baseline = alpha * int(item["count"]) + (1.0 - alpha) * baseline

        threshold = baseline + max(baseline * threshold_pct, min_buffer)
        above = today_count is not None and today_count > threshold
        change_percent = (
            ((today_count - baseline) / baseline) * 100.0
            if today_count is not None and baseline > 0
            else None
        )

        return {
            **base,
            "available": True,
            "baseline": round(baseline, 2),
            "threshold": round(threshold, 2),
            # Backward-compatible aliases used by the existing rule/tests.
            "max_allowed": round(threshold, 2),
            "change_percent": (
                round(change_percent, 1) if change_percent is not None else None
            ),
            "above_baseline": above,
            "abnormal": above,
            "reason": None if today_count is not None else "current_day_missing",
        }
