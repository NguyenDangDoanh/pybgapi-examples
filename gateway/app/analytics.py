"""Per-patient occurrence-time cough-bout analytics."""

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
EWMA_ALPHA = 0.2
EWMA_WARMUP_PERIODS = 7
TREND_TOLERANCE_PCT = 0.10


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp and return it in the configured local timezone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
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


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


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
        key = "day" if DAY_START_HOUR <= occurred.hour < NIGHT_START_HOUR else "night"
        counts[key] += 1
    return counts


def _ten_minute_key(occurred: datetime) -> str:
    """Return the legacy local 10-minute bucket key."""
    bucket = occurred.replace(
        minute=(occurred.minute // 10) * 10, second=0, microsecond=0
    )
    return bucket.strftime("%Y-%m-%dT%H:%M:%S%z")


def _thirty_minute_key(occurred: datetime) -> str:
    bucket = occurred.replace(
        minute=(occurred.minute // 30) * 30, second=0, microsecond=0
    )
    return bucket.strftime("%Y-%m-%dT%H:%M:%S%z")


def _ewma(values: list[int], alpha: float = EWMA_ALPHA) -> float | None:
    if not values:
        return None
    value = float(values[0])
    for count in values[1:]:
        value = alpha * int(count) + (1.0 - alpha) * value
    return value


def _trend(count: int, baseline: float, tolerance: float) -> str:
    """Classify a completed period against its pre-update EWMA snapshot."""
    if baseline == 0:
        return "stable" if count == 0 else "increasing"
    delta = (count - baseline) / baseline
    if delta > tolerance:
        return "increasing"
    if delta < -tolerance:
        return "decreasing"
    return "stable"


class Analytics:
    """Compute occurrence-time bout trends independently for each patient."""

    def __init__(self, dao: Dao) -> None:
        self.dao = dao

    def get_client_stats(self, client_id: str, now: datetime | None = None) -> dict:
        occurrence_events = self.dao.get_events_by_occurrence(
            client_id=client_id, limit=1, descending=True
        )
        last_event_ts = occurrence_events[0].get("event_ts") if occurrence_events else None
        transport_events = self.dao.get_events(client_id=client_id, limit=1)
        last_received_ts = transport_events[0].get("received_ts") if transport_events else None

        if now is None and last_event_ts:
            anchor = _parse_ts(last_event_ts)
            now_utc = anchor.astimezone(timezone.utc) if anchor else _as_utc(None)
        else:
            now_utc = _as_utc(now)
        local_now = now_utc.astimezone(DISPLAY_TIMEZONE)

        start_24h = now_utc - timedelta(hours=24)
        start_7d = _local_midnight_utc(local_now.date() - timedelta(days=6))
        start_history = now_utc - timedelta(days=7)
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
        events_history = self.dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=_iso_utc(start_history),
            end_time=_iso_utc(now_utc),
        )

        hourly: defaultdict[str, int] = defaultdict(int)
        ten_minute: defaultdict[str, int] = defaultdict(int)
        thirty_minute: defaultdict[str, int] = defaultdict(int)
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None:
                continue
            hourly[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
            ten_minute[_ten_minute_key(occurred)] += 1
            thirty_minute[_thirty_minute_key(occurred)] += 1

        daily: defaultdict[str, int] = defaultdict(int)
        for event in events_7d:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                daily[occurred.strftime("%Y-%m-%d")] += 1

        hourly_history: defaultdict[str, int] = defaultdict(int)
        ten_minute_history: defaultdict[str, int] = defaultdict(int)
        thirty_minute_history: defaultdict[str, int] = defaultdict(int)
        for event in events_history:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None:
                continue
            hourly_history[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
            ten_minute_history[_ten_minute_key(occurred)] += 1
            thirty_minute_history[_thirty_minute_key(occurred)] += 1

        timeline = self.personal_ewma_timeline(client_id, now=now_utc)
        baseline = self.ewma_baseline_status(client_id, now=now_utc, _timeline=timeline)
        day_night_trends = self.day_night_ewma_status(client_id, now=now_utc)
        weekly_finding = self.weekly_finding_status(
            client_id, now=now_utc, _timeline=timeline
        )

        by_type_24h = _type_counts(events_24h)
        by_type_7d = _type_counts(events_7d)
        all_events = self.dao.get_events(client_id=client_id)
        return {
            # Compatibility keys retained for existing API consumers.
            "total": len(all_events),
            "by_type": by_type_24h,
            "per_hour": [
                {"ts": key, "count": value} for key, value in sorted(hourly.items())
            ],
            "per_10_minute": [
                {"ts": key, "count": value}
                for key, value in sorted(ten_minute.items())
            ],
            "per_hour_history": [
                {"ts": key, "count": value}
                for key, value in sorted(hourly_history.items())
            ],
            "per_10_minute_history": [
                {"ts": key, "count": value}
                for key, value in sorted(ten_minute_history.items())
            ],
            "per_30_minute": [
                {"ts": key, "count": value}
                for key, value in sorted(thirty_minute.items())
            ],
            "per_30_minute_history": [
                {"ts": key, "count": value}
                for key, value in sorted(thirty_minute_history.items())
            ],
            "per_day": [
                {"date": key, "count": value} for key, value in sorted(daily.items())
            ],
            "last_24h_count": len(events_24h),
            "last_7d_count": len(events_7d),
            "today_count": baseline["today_count"],
            "by_type_24h": by_type_24h,
            "by_type_7d": by_type_7d,
            "day_night_24h": _day_night_counts(events_24h),
            "day_night_7d": _day_night_counts(events_7d),
            "day_night_trends": day_night_trends,
            "baseline": baseline,
            "personal_ewma": timeline,
            "weekly_finding": weekly_finding,
            "monitoring_start_date": timeline["start_date"],
            "last_event_ts": last_event_ts,
            "last_received_ts": last_received_ts,
            "analysis_anchor_ts": _iso_utc(now_utc),
        }

    def daily_cough_counts(
        self,
        client_id: str,
        days: int | None = None,
        now: datetime | None = None,
    ) -> list[dict]:
        """Return observed bout counts by local day; missing days stay unknown."""
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
            client_id=client_id, start_time=start_time, end_time=_iso_utc(now_utc)
        )
        counts: defaultdict[str, int] = defaultdict(int)
        for event in events:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                counts[occurred.strftime("%Y-%m-%d")] += 1
        return [{"date": key, "count": value} for key, value in sorted(counts.items())]

    def personal_ewma_timeline(
        self,
        client_id: str,
        alpha: float = EWMA_ALPHA,
        warmup_days: int = EWMA_WARMUP_PERIODS,
        now: datetime | None = None,
    ) -> dict:
        """Build the long-running Personal EWMA from completed usable days.

        The automatic start date is the first valid event date. That first date
        is conservatively marked partial and is not a usable warm-up day. Dates
        with no evidence remain missing rather than being invented as zero days.
        """
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in the range (0, 1]")
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")

        now_utc = _as_utc(now)
        today = now_utc.astimezone(DISPLAY_TIMEZONE).date()
        observed = self.daily_cough_counts(client_id, now=now_utc)
        empty = {
            "client_id": client_id,
            "start_date": None,
            "today": today.isoformat(),
            "alpha": alpha,
            "warmup_days": warmup_days,
            "completed_usable_days": 0,
            "warmup_remaining": warmup_days,
            "current_baseline": None,
            "days": [],
        }
        if not observed:
            return empty

        start_date = date.fromisoformat(observed[0]["date"])
        days = [
            {
                "date": observed[0]["date"],
                "count": int(observed[0]["count"]),
                "complete": start_date < today,
                "usable": False,
                "phase": "start_partial",
                "baseline_before": None,
                "baseline_after": None,
            }
        ]
        completed = [
            item
            for item in observed[1:]
            if date.fromisoformat(item["date"]) < today
        ]
        ewma: float | None = None
        for index, item in enumerate(completed):
            count = int(item["count"])
            baseline_before = ewma if index >= warmup_days else None
            ewma = count if ewma is None else alpha * count + (1.0 - alpha) * ewma
            days.append(
                {
                    "date": item["date"],
                    "count": count,
                    "complete": True,
                    "usable": True,
                    "phase": "warmup" if index < warmup_days else "monitoring",
                    "baseline_before": _rounded(baseline_before),
                    "baseline_after": _rounded(ewma),
                }
            )

        current = next(
            (
                item
                for item in observed[1:]
                if date.fromisoformat(item["date"]) == today
            ),
            None,
        )
        if current is not None:
            days.append(
                {
                    "date": current["date"],
                    "count": int(current["count"]),
                    "complete": False,
                    "usable": False,
                    "phase": "current",
                    "baseline_before": (
                        _rounded(ewma) if len(completed) >= warmup_days else None
                    ),
                    "baseline_after": None,
                }
            )

        completed_count = len(completed)
        return {
            **empty,
            "start_date": start_date.isoformat(),
            "completed_usable_days": completed_count,
            "warmup_remaining": max(warmup_days - completed_count, 0),
            "current_baseline": (
                _rounded(ewma) if completed_count >= warmup_days else None
            ),
            "days": sorted(days, key=lambda item: item["date"]),
        }

    def ewma_baseline_status(
        self,
        client_id: str,
        alpha: float = EWMA_ALPHA,
        threshold_pct: float = 0.4,
        min_buffer: float = 5.0,
        warmup_days: int = EWMA_WARMUP_PERIODS,
        now: datetime | None = None,
        _timeline: dict | None = None,
    ) -> dict:
        """Compare the active day's count with the pre-update Personal EWMA."""
        if threshold_pct < 0 or min_buffer < 0:
            raise ValueError("threshold and buffer must be non-negative")
        timeline = _timeline or self.personal_ewma_timeline(
            client_id, alpha=alpha, warmup_days=warmup_days, now=now
        )
        today_item = next(
            (item for item in timeline["days"] if item["date"] == timeline["today"]),
            None,
        )
        today_count = int(today_item["count"]) if today_item else None
        baseline = timeline["current_baseline"]
        base = {
            "client_id": client_id,
            "today": timeline["today"],
            "today_count": today_count,
            "current_available": today_count is not None,
            "observed_history_days": timeline["completed_usable_days"],
            "warmup_days": warmup_days,
            "warmup_remaining": timeline["warmup_remaining"],
            "alpha": alpha,
            "threshold_pct": threshold_pct,
            "min_buffer": min_buffer,
        }
        if baseline is None:
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
        threshold = baseline + max(baseline * threshold_pct, min_buffer)
        change = (
            ((today_count - baseline) / baseline) * 100
            if today_count is not None and baseline > 0
            else None
        )
        above = today_count is not None and today_count > threshold
        return {
            **base,
            "available": True,
            "baseline": _rounded(baseline),
            "threshold": _rounded(threshold),
            "max_allowed": _rounded(threshold),
            "change_percent": round(change, 1) if change is not None else None,
            "above_baseline": above,
            "abnormal": above,
            "reason": None if today_count is not None else "current_day_missing",
        }

    def weekly_finding_status(
        self,
        client_id: str,
        alpha: float = EWMA_ALPHA,
        warmup_days: int = EWMA_WARMUP_PERIODS,
        now: datetime | None = None,
        _timeline: dict | None = None,
    ) -> dict:
        """Return the active seven-usable-day block and latest completed result."""
        timeline = _timeline or self.personal_ewma_timeline(
            client_id, alpha=alpha, warmup_days=warmup_days, now=now
        )
        usable = [
            item for item in timeline["days"] if item.get("complete") and item.get("usable")
        ]
        completed_weeks: list[dict] = []
        for offset in range(7, len(usable) - 6, 7):
            group = usable[offset : offset + 7]
            if len(group) < 7:
                break
            week_number = offset // 7 + 1
            reference = _ewma(
                [int(item["count"]) for item in usable[:offset]], alpha
            )
            weekly_level = _ewma([int(item["count"]) for item in group], alpha)
            change = (
                ((weekly_level - reference) / reference) * 100
                if weekly_level is not None and reference is not None and reference > 0
                else None
            )
            completed_weeks.append(
                {
                    "week": week_number,
                    "weekly_level": _rounded(weekly_level),
                    "week_reference_snapshot": _rounded(reference),
                    "change_percent": round(change, 1) if change is not None else None,
                    "change_available": change is not None,
                    "start_date": group[0]["date"],
                    "end_date": group[-1]["date"],
                }
            )

        completed_count = len(usable)
        active_week = completed_count // 7 + 1
        days_in_week = completed_count % 7
        active_offset = (active_week - 1) * 7
        active_reference = (
            _ewma(
                [int(item["count"]) for item in usable[:active_offset]], alpha
            )
            if active_week > 1
            else None
        )
        return {
            "client_id": client_id,
            "start_date": timeline["start_date"],
            "active_week": active_week,
            "completed_days_in_week": days_in_week,
            "warmup": active_week == 1,
            "status": "baseline_formation" if active_week == 1 else "calculating",
            "week_reference_snapshot": _rounded(active_reference),
            "latest_completed_period": completed_weeks[-1] if completed_weeks else None,
        }

    def day_night_ewma_status(
        self,
        client_id: str,
        alpha: float = EWMA_ALPHA,
        warmup_periods: int = EWMA_WARMUP_PERIODS,
        tolerance_pct: float = TREND_TOLERANCE_PCT,
        now: datetime | None = None,
    ) -> dict:
        """Maintain independent Day and Night EWMA streams for hover trends."""
        if warmup_periods <= 0:
            raise ValueError("warmup_periods must be positive")
        if tolerance_pct < 0:
            raise ValueError("tolerance_pct must be non-negative")
        now_utc = _as_utc(now)
        local_now = now_utc.astimezone(DISPLAY_TIMEZONE)
        events = self.dao.get_events_by_occurrence(
            client_id=client_id, end_time=_iso_utc(now_utc)
        )
        streams: dict[str, defaultdict[date, int]] = {
            "day": defaultdict(int),
            "night": defaultdict(int),
        }
        for event in events:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is None:
                continue
            if DAY_START_HOUR <= occurred.hour < NIGHT_START_HOUR:
                streams["day"][occurred.date()] += 1
            else:
                night_start = (
                    occurred.date()
                    if occurred.hour >= NIGHT_START_HOUR
                    else occurred.date() - timedelta(days=1)
                )
                streams["night"][night_start] += 1

        current_day_key = local_now.date()
        current_night_key = (
            local_now.date()
            if local_now.hour >= NIGHT_START_HOUR
            else local_now.date() - timedelta(days=1)
        )
        active_stream = (
            "day" if DAY_START_HOUR <= local_now.hour < NIGHT_START_HOUR else "night"
        )
        result = {}
        for name, counts in streams.items():
            keys = sorted(counts)
            if not keys:
                result[name] = {"trend": None, "reason": "no_data"}
                continue
            if name == "day":
                complete_keys = [
                    key
                    for key in keys[1:]
                    if key < current_day_key
                    or (key == current_day_key and local_now.hour >= NIGHT_START_HOUR)
                ]
            else:
                complete_keys = [
                    key
                    for key in keys[1:]
                    if key < current_night_key
                    or (key == current_night_key and active_stream == "day")
                ]
            values = [counts[key] for key in complete_keys]
            is_active = name == active_stream
            if is_active:
                result[name] = {
                    "trend": None,
                    "reason": "active_period",
                    "warmup_remaining": max(warmup_periods - len(values), 0),
                }
                continue
            if len(values) <= warmup_periods:
                result[name] = {
                    "trend": None,
                    "reason": "warmup",
                    "warmup_remaining": max(warmup_periods + 1 - len(values), 0),
                }
                continue
            baseline = _ewma(values[:-1], alpha)
            latest_count = values[-1]
            result[name] = {
                "trend": _trend(latest_count, float(baseline), tolerance_pct),
                "reason": None,
                "period_start": complete_keys[-1].isoformat(),
                "count": latest_count,
            }
        return result

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
        counts: defaultdict[str, int] = defaultdict(int)
        for event in events:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                counts[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
        return [{"ts": key, "count": value} for key, value in sorted(counts.items())]

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
