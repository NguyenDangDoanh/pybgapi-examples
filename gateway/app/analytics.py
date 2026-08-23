"""Occurrence-time cough aggregates, EWMA baseline, and warning rules."""

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
EWMA_ALPHA = 0.2
EWMA_WARMUP_DAYS = 7
THRESHOLD_PERCENT = 0.4
THRESHOLD_MIN_BUFFER = 5.0


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp into the configured local timezone."""
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


def _type_name(event: dict) -> str:
    cough_type = str(event.get("cough_type") or "unknown").lower()
    return cough_type if cough_type in {"dry", "wet", "unknown"} else "unknown"


def _type_counts(events: list[dict]) -> dict[str, int]:
    counts = {"wet": 0, "dry": 0, "unknown": 0}
    for event in events:
        counts[_type_name(event)] += 1
    return counts


def _day_night_counts(events: list[dict]) -> dict[str, int]:
    counts = {"day": 0, "night": 0}
    for event in events:
        occurred = _parse_ts(event.get("event_ts"))
        if occurred is None:
            continue
        period = "day" if DAY_START_HOUR <= occurred.hour < NIGHT_START_HOUR else "night"
        counts[period] += 1
    return counts


def _ten_minute_key(occurred: datetime) -> str:
    bucket = occurred.replace(
        minute=(occurred.minute // 10) * 10, second=0, microsecond=0
    )
    return bucket.strftime("%Y-%m-%dT%H:%M:%S%z")


def _thirty_minute_start(occurred: datetime) -> datetime:
    return occurred.replace(
        minute=(occurred.minute // 30) * 30, second=0, microsecond=0
    )


def _ewma_value(items: list[dict], alpha: float = EWMA_ALPHA) -> float | None:
    if not items:
        return None
    value = float(items[0]["count"])
    for item in items[1:]:
        value = alpha * int(item["count"]) + (1.0 - alpha) * value
    return value


class Analytics:
    """Compute patient analytics from event occurrence time, never receipt time."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def get_client_stats(self, client_id: str, now: datetime | None = None) -> dict:
        now_utc = _as_utc(now)
        local_now = now_utc.astimezone(DISPLAY_TIMEZONE)
        start_24h = now_utc - timedelta(hours=24)
        seven_day_start = local_now.date() - timedelta(days=7)
        seven_day_end = local_now.date() - timedelta(days=1)

        latest_events = self.dao.get_events_by_occurrence(
            client_id=client_id, limit=1, descending=True
        )
        first_events = self.dao.get_events_by_occurrence(client_id=client_id, limit=1)
        transport_events = self.dao.get_events(client_id=client_id, limit=1)
        last_event_ts = latest_events[0].get("event_ts") if latest_events else None
        first_local = _parse_ts(first_events[0].get("event_ts")) if first_events else None
        first_data_date = first_local.date() if first_local else None
        last_received_ts = transport_events[0].get("received_ts") if transport_events else None

        events_24h = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_24h),
            end_time=_iso_utc(now_utc),
        )
        events_7d = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(_local_midnight_utc(seven_day_start)),
            end_time=_iso_utc(
                _local_midnight_utc(local_now.date()) - timedelta(milliseconds=1)
            ),
        )

        hourly_map: defaultdict[str, int] = defaultdict(int)
        ten_minute_map: defaultdict[str, int] = defaultdict(int)
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_map[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
                ten_minute_map[_ten_minute_key(occurred)] += 1

        thirty_minute_map: dict[str, dict[str, int | str]] = {}
        cursor = _thirty_minute_start(start_24h.astimezone(DISPLAY_TIMEZONE))
        bucket_end = _thirty_minute_start(local_now)
        while cursor <= bucket_end:
            key = cursor.strftime("%Y-%m-%dT%H:%M:%S%z")
            thirty_minute_map[key] = {
                "ts": key,
                "dry": 0,
                "wet": 0,
                "unknown": 0,
                "total": 0,
            }
            cursor += timedelta(minutes=30)
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None:
                continue
            key = _thirty_minute_start(occurred).strftime("%Y-%m-%dT%H:%M:%S%z")
            bucket = thirty_minute_map.get(key)
            if bucket is None:
                continue
            cough_type = _type_name(event)
            bucket[cough_type] = int(bucket[cough_type]) + 1
            bucket["total"] = int(bucket["total"]) + 1

        # Dates with no events are unavailable monitoring data, not zero-cough days.
        daily_map: dict[date, dict[str, object]] = {}
        for event in events_7d:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None:
                continue
            event_day = occurred.date()
            item = daily_map.setdefault(
                event_day,
                {
                    "date": event_day.isoformat(),
                    "day": 0,
                    "night": 0,
                    "dry": 0,
                    "wet": 0,
                    "unknown": 0,
                    "total": 0,
                    "day_types": {"dry": 0, "wet": 0, "unknown": 0},
                    "night_types": {"dry": 0, "wet": 0, "unknown": 0},
                },
            )
            cough_type = _type_name(event)
            period = "day" if DAY_START_HOUR <= occurred.hour < NIGHT_START_HOUR else "night"
            item[cough_type] = int(item[cough_type]) + 1
            item[period] = int(item[period]) + 1
            item["total"] = int(item["total"]) + 1
            period_types = item[f"{period}_types"]
            assert isinstance(period_types, dict)
            period_types[cough_type] = int(period_types[cough_type]) + 1

        baseline = self.ewma_baseline_status(client_id, now=now_utc)
        treatment = self.treatment_response_status(client_id, now=now_utc)
        all_events = self.dao.get_events(client_id=client_id)
        per_day = [daily_map[key] for key in sorted(daily_map)]
        warning = {
            key: baseline.get(key)
            for key in (
                "warning_level",
                "warning_label",
                "baseline",
                "threshold",
                "c24",
                "ratio",
                "consecutive_abnormal_days",
            )
        }
        return {
            "total": len(all_events),
            "by_type": _type_counts(events_24h),
            "per_hour": [
                {"ts": key, "count": value}
                for key, value in sorted(hourly_map.items())
            ],
            "per_10_minute": [
                {"ts": key, "count": value}
                for key, value in sorted(ten_minute_map.items())
            ],
            "per_30_minute": list(thirty_minute_map.values()),
            "per_day": per_day,
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
            "by_type_24h": _type_counts(events_24h),
            "by_type_7d": _type_counts(events_7d),
            "day_night_24h": _day_night_counts(events_24h),
            "day_night_7d": _day_night_counts(events_7d),
            "baseline": baseline,
            "warning": warning,
            "treatment_response": treatment,
            "last_event_ts": last_event_ts,
            "first_data_date": first_data_date.isoformat() if first_data_date else None,
            "completed_7d_available": len(per_day) == 7,
            "observed_7d_days": len(per_day),
            "window_24h_start": _iso_utc(start_24h),
            "window_24h_end": _iso_utc(now_utc),
            "window_7d_start": seven_day_start.isoformat(),
            "window_7d_end": seven_day_end.isoformat(),
            "last_received_ts": last_received_ts,
            "analysis_anchor_ts": _iso_utc(now_utc),
        }

    def event_dates(self, client_id: str) -> dict:
        dates = sorted(
            {
                occurred.date().isoformat()
                for value in self.dao.get_event_timestamps(client_id)
                if (occurred := _parse_ts(value)) is not None
            },
        )
        return {
            "client_id": client_id,
            "dates": dates,
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
        }

    def treatment_response_status(
        self,
        client_id: str,
        treatment_start_date: str | None = None,
        warmup_days: int = EWMA_WARMUP_DAYS,
        now: datetime | None = None,
    ) -> dict:
        """Compare a completed treatment week with its start-of-week EWMA."""
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")
        now_utc = _as_utc(now)
        local_today = now_utc.astimezone(DISPLAY_TIMEZONE).date()
        daily = self.daily_cough_counts(client_id=client_id, now=now_utc)
        counts = {date.fromisoformat(item["date"]): int(item["count"]) for item in daily}
        base = {
            "client_id": client_id,
            "treatment_start_date": None,
            "first_data_date": None,
            "warmup_days": warmup_days,
            "available": False,
            "ewma_reference": None,
            "current": None,
            "change_percent": None,
            "direction": None,
            "reference_observed_days": 0,
            "evaluation_observed_days": 0,
            "warmup_remaining": warmup_days,
            "current_week_number": None,
            "current_week_start": None,
            "current_week_end": None,
            "current_week_complete": False,
            "current_week_observed_days": 0,
            "current_week_average": None,
            "evaluation_week_number": None,
            "evaluation_week_start": None,
            "evaluation_week_end": None,
        }
        if not counts:
            return {**base, "reason": "no_data"}

        first_data_day = min(counts)
        span_days = max((local_today - first_data_day).days, 0)
        current_week_number = span_days // 7 + 1
        current_week_start = first_data_day + timedelta(days=(current_week_number - 1) * 7)
        current_week_end = current_week_start + timedelta(days=6)
        current_week_values = [
            count
            for day, count in counts.items()
            if current_week_start <= day <= min(current_week_end, local_today)
        ]
        automatic = {
            **base,
            "treatment_start_date": first_data_day.isoformat(),
            "first_data_date": first_data_day.isoformat(),
            "current_week_number": current_week_number,
            "current_week_start": current_week_start.isoformat(),
            "current_week_end": current_week_end.isoformat(),
            "current_week_observed_days": len(current_week_values),
            "current_week_average": round(sum(current_week_values) / len(current_week_values), 2)
            if current_week_values
            else None,
        }
        completed_week_count = span_days // 7
        completed_observed = [
            {"date": day.isoformat(), "count": count}
            for day, count in sorted(counts.items())
            if day < local_today
        ]
        if completed_week_count == 0:
            observed = len(completed_observed)
            return {
                **automatic,
                "reference_observed_days": observed,
                "warmup_remaining": max(warmup_days - observed, 0),
                "reason": "warmup",
            }
        if completed_week_count == 1:
            return {
                **automatic,
                "reference_observed_days": len(completed_observed),
                "warmup_remaining": max(warmup_days - len(completed_observed), 0),
                "reason": "awaiting_completed_comparison_week",
            }

        evaluation_week = completed_week_count
        evaluation_start = first_data_day + timedelta(days=(evaluation_week - 1) * 7)
        evaluation_end = evaluation_start + timedelta(days=6)
        reference_items = [
            item
            for item in completed_observed
            if date.fromisoformat(item["date"]) < evaluation_start
        ]
        evaluation_values = [
            count
            for day, count in sorted(counts.items())
            if evaluation_start <= day <= evaluation_end
        ]
        reference = _ewma_value(reference_items)
        current = sum(evaluation_values) / len(evaluation_values) if evaluation_values else None
        result = {
            **automatic,
            "ewma_reference": round(reference, 2) if reference is not None else None,
            "current": round(current, 2) if current is not None else None,
            "reference_observed_days": len(reference_items),
            "evaluation_observed_days": len(evaluation_values),
            "warmup_remaining": max(warmup_days - len(reference_items), 0),
            "evaluation_week_number": evaluation_week,
            "evaluation_week_start": evaluation_start.isoformat(),
            "evaluation_week_end": evaluation_end.isoformat(),
        }
        if len(reference_items) < warmup_days:
            return {**result, "reason": "reference_warmup"}
        if len(evaluation_values) < 7:
            return {**result, "reason": "incomplete_monitoring"}
        if reference is None or reference <= 0:
            return {**result, "reason": "zero_reference"}

        change = ((current - reference) / reference) * 100.0
        direction = (
            "decreased" if current < reference else "increased" if current > reference else "unchanged"
        )
        return {
            **result,
            "available": True,
            "change_percent": round(change, 1),
            "direction": direction,
            "reason": None,
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
        return [{"ts": key, "count": value} for key, value in sorted(hourly_map.items())]

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
        return [{"date": key, "count": value} for key, value in sorted(daily_map.items())]

    def ewma_baseline_status(
        self,
        client_id: str,
        alpha: float = EWMA_ALPHA,
        threshold_pct: float = THRESHOLD_PERCENT,
        min_buffer: float = THRESHOLD_MIN_BUFFER,
        warmup_days: int = EWMA_WARMUP_DAYS,
        now: datetime | None = None,
    ) -> dict:
        """Compare rolling 24-hour bouts with the completed-day Personal EWMA."""
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in the range (0, 1]")
        if threshold_pct < 0.0:
            raise ValueError("threshold_pct must be non-negative")
        if min_buffer < 0.0:
            raise ValueError("min_buffer must be non-negative")
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")

        now_utc = _as_utc(now)
        today = now_utc.astimezone(DISPLAY_TIMEZONE).date()
        daily_counts = self.daily_cough_counts(client_id=client_id, now=now_utc)
        today_count = next(
            (int(item["count"]) for item in daily_counts if item["date"] == today.isoformat()),
            None,
        )
        completed_days = [item for item in daily_counts if item["date"] < today.isoformat()]
        c24 = len(
            self.dao.get_events_by_occurrence(
                client_id=client_id,
                start_time=_iso_utc(now_utc - timedelta(hours=24)),
                end_time=_iso_utc(now_utc),
            )
        )
        base = {
            "client_id": client_id,
            "today": today.isoformat(),
            "today_count": today_count,
            "current_available": bool(daily_counts),
            "current_24h": c24,
            "c24": c24,
            "observed_history_days": len(completed_days),
            "warmup_days": warmup_days,
            "warmup_remaining": max(warmup_days - len(completed_days), 0),
            "alpha": alpha,
            "threshold_pct": threshold_pct,
            "min_buffer": min_buffer,
            "updated_through": completed_days[-1]["date"] if completed_days else None,
            "consecutive_abnormal_days": 0,
            "ratio": None,
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
                "warning_level": "calibrating",
                "warning_label": "Calibrating",
                "reason": "warmup",
            }

        running: float | None = None
        observed = 0
        consecutive = 0
        for item in completed_days:
            count = int(item["count"])
            if running is not None and observed >= warmup_days:
                historical_threshold = running + max(
                    running * threshold_pct, min_buffer
                )
                consecutive = consecutive + 1 if count > historical_threshold else 0
            running = (
                float(count)
                if running is None
                else alpha * count + (1.0 - alpha) * running
            )
            observed += 1
        assert running is not None
        threshold = running + max(running * threshold_pct, min_buffer)
        ratio = c24 / running if running > 0 else None
        change = ((c24 - running) / running) * 100.0 if running > 0 else None
        above = c24 > threshold
        if not above:
            warning_level, warning_label = "normal", "Normal"
        elif (ratio is not None and ratio >= 2.0) or consecutive >= 2:
            warning_level, warning_label = "high_priority", "High priority"
        else:
            warning_level, warning_label = "needs_review", "Needs review"
        return {
            **base,
            "available": True,
            "baseline": round(running, 2),
            "threshold": round(threshold, 2),
            "max_allowed": round(threshold, 2),
            "change_percent": round(change, 1) if change is not None else None,
            "above_baseline": above,
            "abnormal": above,
            "warning_level": warning_level,
            "warning_label": warning_label,
            "ratio": round(ratio, 3) if ratio is not None else None,
            "consecutive_abnormal_days": consecutive,
            "reason": None,
        }
