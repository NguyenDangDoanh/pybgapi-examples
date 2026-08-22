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
NIGHT_START_HOUR = 22


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


def _thirty_minute_start(occurred: datetime) -> datetime:
    """Floor a local timestamp to a clock-aligned :00/:30 bucket."""
    return occurred.replace(
        minute=(occurred.minute // 30) * 30,
        second=0,
        microsecond=0,
    )


def _type_name(event: dict) -> str:
    cough_type = str(event.get("cough_type") or "unknown").lower()
    return cough_type if cough_type in {"dry", "wet", "unknown"} else "unknown"


class Analytics:
    """Compute occurrence-time bout trends independently for each client."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def get_client_stats(
        self, client_id: str, now: datetime | None = None
    ) -> dict:
        latest_events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            limit=1,
            descending=True,
        )
        last_event_ts = (
            latest_events[0].get("event_ts") if latest_events else None
        )
        first_events = self.dao.get_events_by_occurrence(
            client_id=client_id,
            limit=1,
        )
        first_event = first_events[0] if first_events else None
        first_event_local = _parse_ts(first_event.get("event_ts")) if first_event else None
        first_data_date = first_event_local.date() if first_event_local else None
        transport_events = self.dao.get_events(client_id=client_id, limit=1)
        last_received_ts = (
            transport_events[0].get("received_ts") if transport_events else None
        )
        # A quiet period is clinically meaningful monitoring time.  The rolling
        # window therefore follows wall-clock time rather than the last cough.
        now_utc = _as_utc(now)
        local_now = now_utc.astimezone(DISPLAY_TIMEZONE)
        start_24h = now_utc - timedelta(hours=24)
        seven_day_start = local_now.date() - timedelta(days=7)
        seven_day_end = local_now.date() - timedelta(days=1)
        start_7d = _local_midnight_utc(seven_day_start)
        end_7d_exclusive = _local_midnight_utc(local_now.date())

        events_24h = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_24h),
            end_time=_iso_utc(now_utc),
        )
        events_7d = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_7d),
            end_time=_iso_utc(end_7d_exclusive - timedelta(milliseconds=1)),
        )

        hourly_map: defaultdict[str, int] = defaultdict(int)
        ten_minute_map: defaultdict[str, int] = defaultdict(int)
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_map[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
                ten_minute_map[_ten_minute_key(occurred)] += 1

        bucket_cursor = _thirty_minute_start(
            start_24h.astimezone(DISPLAY_TIMEZONE)
        )
        bucket_end = _thirty_minute_start(local_now)
        thirty_minute_map: dict[str, dict[str, int | str]] = {}
        while bucket_cursor <= bucket_end:
            key = bucket_cursor.strftime("%Y-%m-%dT%H:%M:%S%z")
            thirty_minute_map[key] = {
                "ts": key,
                "dry": 0,
                "wet": 0,
                "unknown": 0,
                "total": 0,
            }
            bucket_cursor += timedelta(minutes=30)
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None:
                continue
            key = _thirty_minute_start(occurred).strftime(
                "%Y-%m-%dT%H:%M:%S%z"
            )
            bucket = thirty_minute_map.get(key)
            if bucket is None:
                continue
            cough_type = _type_name(event)
            bucket[cough_type] = int(bucket[cough_type]) + 1
            bucket["total"] = int(bucket["total"]) + 1

        daily_map: dict[date, dict[str, int | str]] = {}
        display_start = seven_day_start
        if first_data_date is not None and first_data_date > display_start:
            display_start = first_data_date
        cursor = display_start
        while cursor <= seven_day_end:
            daily_map[cursor] = {
                "date": cursor.isoformat(),
                "day": 0,
                "night": 0,
                "dry": 0,
                "wet": 0,
                "unknown": 0,
                "total": 0,
            }
            cursor += timedelta(days=1)
        for event in events_7d:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None or occurred.date() not in daily_map:
                continue
            item = daily_map[occurred.date()]
            cough_type = _type_name(event)
            period = (
                "day"
                if DAY_START_HOUR <= occurred.hour < NIGHT_START_HOUR
                else "night"
            )
            item[cough_type] = int(item[cough_type]) + 1
            item[period] = int(item[period]) + 1
            item["total"] = int(item["total"]) + 1

        all_events = self.dao.get_events(client_id=client_id)
        baseline = self.ewma_baseline_status(client_id, now=now_utc)
        treatment_response = self.treatment_response_status(
            client_id,
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
            "per_30_minute": list(thirty_minute_map.values()),
            "per_day": list(daily_map.values()),
            # Deprecated aliases retained for older API consumers.  The
            # dashboard itself uses per_30_minute and per_day above.
            "per_hour_history": [
                {"ts": key, "count": value}
                for key, value in sorted(hourly_map.items())
            ],
            "per_10_minute_history": [
                {"ts": key, "count": value}
                for key, value in sorted(ten_minute_map.items())
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
            "first_data_date": (
                first_data_date.isoformat() if first_data_date is not None else None
            ),
            "completed_7d_available": len(daily_map) == 7,
            "window_24h_start": _iso_utc(start_24h),
            "window_24h_end": _iso_utc(now_utc),
            "window_7d_start": seven_day_start.isoformat(),
            "window_7d_end": seven_day_end.isoformat(),
            # Transport receipt remains available to API consumers for audit
            # and reconnect diagnostics, but does not drive patient analytics.
            "last_received_ts": last_received_ts,
            "analysis_anchor_ts": _iso_utc(now_utc),
        }

    def treatment_response_status(
        self,
        client_id: str,
        treatment_start_date: str | None = None,
        warmup_days: int = 7,
        now: datetime | None = None,
    ) -> dict:
        """Compare completed seven-day treatment weeks.

        Treatment Day 1 is the first valid recorded local day.  Week 1 builds
        the initial baseline.  Week 2 is compared with Week 1, while Week 3+
        use every completed prior treatment day as the expanding personal
        baseline.  A partial current week is never compared quantitatively.

        ``treatment_start_date`` is retained as a deprecated call-compatible
        argument but is intentionally ignored; the start is data-derived.
        """
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")

        base = {
            "client_id": client_id,
            "treatment_start_date": None,
            "first_data_date": None,
            "warmup_days": warmup_days,
            "available": False,
            "baseline": None,
            "current": None,
            "change_percent": None,
            "direction": None,
            "baseline_days": 0,
            "warmup_remaining": warmup_days,
            "current_week_number": None,
            "current_week_start": None,
            "current_week_end": None,
            "current_week_complete": False,
            "evaluation_week_number": None,
            "evaluation_week_start": None,
            "evaluation_week_end": None,
        }

        now_utc = _as_utc(now)
        local_today = now_utc.astimezone(DISPLAY_TIMEZONE).date()
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

        first_data_day = min(day for day, _event in dated_events)
        treatment_span_days = max((local_today - first_data_day).days, 0)
        current_week_number = treatment_span_days // 7 + 1
        current_week_start = first_data_day + timedelta(
            days=(current_week_number - 1) * 7
        )
        current_week_end = current_week_start + timedelta(days=6)
        completed_week_count = treatment_span_days // 7
        automatic = {
            **base,
            "treatment_start_date": first_data_day.isoformat(),
            "first_data_date": first_data_day.isoformat(),
            "current_week_number": current_week_number,
            "current_week_start": current_week_start.isoformat(),
            "current_week_end": current_week_end.isoformat(),
        }
        counts: defaultdict[date, int] = defaultdict(int)
        for day, _event in dated_events:
            if first_data_day <= day < local_today:
                counts[day] += 1

        if completed_week_count == 0:
            completed_days = min(treatment_span_days, 7)
            return {
                **automatic,
                "baseline_days": completed_days,
                "warmup_remaining": max(7 - completed_days, 0),
                "reason": "warmup",
            }

        if completed_week_count == 1:
            week_one_total = sum(
                counts[first_data_day + timedelta(days=offset)]
                for offset in range(7)
            )
            return {
                **automatic,
                "baseline": round(week_one_total / 7.0, 2),
                "baseline_days": 7,
                "warmup_remaining": 0,
                "reason": "awaiting_completed_comparison_week",
            }

        evaluation_week = completed_week_count
        evaluation_start = first_data_day + timedelta(days=(evaluation_week - 1) * 7)
        evaluation_end = evaluation_start + timedelta(days=6)
        baseline_days = (evaluation_week - 1) * 7
        baseline_total = sum(
            counts[first_data_day + timedelta(days=offset)]
            for offset in range(baseline_days)
        )
        evaluation_total = sum(
            counts[evaluation_start + timedelta(days=offset)]
            for offset in range(7)
        )
        cumulative_baseline = baseline_total / baseline_days
        current_count = evaluation_total / 7.0
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
            **automatic,
            "available": True,
            "baseline": round(cumulative_baseline, 2),
            "current": round(current_count, 2),
            "baseline_days": baseline_days,
            "warmup_remaining": 0,
            "evaluation_week_number": evaluation_week,
            "evaluation_week_start": evaluation_start.isoformat(),
            "evaluation_week_end": evaluation_end.isoformat(),
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
            "updated_through": (
                completed_days[-1]["date"] if completed_days else None
            ),
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
