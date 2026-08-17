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
    """Return the local start timestamp of the legacy 10-minute bucket."""
    bucket = occurred.replace(
        minute=(occurred.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    return bucket.strftime("%Y-%m-%dT%H:%M:%S%z")


def _thirty_minute_key(occurred: datetime) -> str:
    """Return the local start timestamp of the event's 30-minute bucket."""
    bucket = occurred.replace(
        minute=(occurred.minute // 30) * 30,
        second=0,
        microsecond=0,
    )
    return bucket.strftime("%Y-%m-%dT%H:%M:%S%z")


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _typed_bucket_rows(
    buckets: dict[str, dict[str, int]],
) -> list[dict]:
    rows: list[dict] = []
    for key, counts in sorted(buckets.items()):
        row = {
            "ts": key,
            "wet": int(counts.get("wet", 0)),
            "dry": int(counts.get("dry", 0)),
            "unknown": int(counts.get("unknown", 0)),
        }
        row["total"] = row["wet"] + row["dry"] + row["unknown"]
        rows.append(row)
    return rows


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
        thirty_minute_map: defaultdict[str, dict[str, int]] = defaultdict(
            lambda: {"wet": 0, "dry": 0, "unknown": 0}
        )
        for event in events_24h:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_map[occurred.strftime("%Y-%m-%dT%H:00:00%z")] += 1
                ten_minute_map[_ten_minute_key(occurred)] += 1
                cough_type = str(event.get("cough_type") or "unknown").lower()
                if cough_type not in {"wet", "dry", "unknown"}:
                    cough_type = "unknown"
                thirty_minute_map[_thirty_minute_key(occurred)][cough_type] += 1

        daily_map: defaultdict[str, int] = defaultdict(int)
        for event in events_7d:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                daily_map[occurred.strftime("%Y-%m-%d")] += 1

        hourly_history_map: defaultdict[str, int] = defaultdict(int)
        ten_minute_history_map: defaultdict[str, int] = defaultdict(int)
        thirty_minute_history_map: defaultdict[str, dict[str, int]] = defaultdict(
            lambda: {"wet": 0, "dry": 0, "unknown": 0}
        )
        for event in events_hour_history:
            occurred = _parse_ts(event.get("event_ts"))
            if occurred is not None:
                hourly_history_map[
                    occurred.strftime("%Y-%m-%dT%H:00:00%z")
                ] += 1
                ten_minute_history_map[_ten_minute_key(occurred)] += 1
                cough_type = str(event.get("cough_type") or "unknown").lower()
                if cough_type not in {"wet", "dry", "unknown"}:
                    cough_type = "unknown"
                thirty_minute_history_map[_thirty_minute_key(occurred)][
                    cough_type
                ] += 1

        all_events = self.dao.get_events(client_id=client_id)
        ewma_timeline = self.personal_ewma_timeline(client_id, now=now_utc)
        baseline = self.ewma_baseline_status(
            client_id,
            now=now_utc,
            _timeline=ewma_timeline,
        )
        monitoring_progress = self.monitoring_progress_status(
            client_id,
            now=now_utc,
            _timeline=ewma_timeline,
        )
        ewma_by_date = {
            item["date"]: item.get("display_baseline")
            for item in ewma_timeline["days"]
        }

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
                {
                    "date": key,
                    "count": value,
                    "ewma_baseline": ewma_by_date.get(key),
                }
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
            "per_30_minute": _typed_bucket_rows(thirty_minute_map),
            "per_30_minute_history": _typed_bucket_rows(
                thirty_minute_history_map
            ),
            "last_24h_count": len(events_24h),
            "last_7d_count": len(events_7d),
            "today_count": baseline["today_count"],
            "by_type_24h": by_type_24h,
            "by_type_7d": by_type_7d,
            "day_night_24h": day_night_24h,
            "day_night_7d": day_night_7d,
            "baseline": baseline,
            "monitoring_progress": monitoring_progress,
            "last_event_ts": last_event_ts,
            # Transport receipt remains available to API consumers for audit
            # and reconnect diagnostics, but does not drive patient analytics.
            "last_received_ts": last_received_ts,
            "analysis_anchor_ts": _iso_utc(now_utc),
        }

    def personal_ewma_timeline(
        self,
        client_id: str,
        alpha: float = 0.2,
        warmup_days: int = 7,
        now: datetime | None = None,
    ) -> dict:
        """Return the single patient EWMA trajectory using completed usable days.

        The first event date is the automatic monitoring start marker and is
        conservatively excluded because coverage before its first event is
        unknown. Missing dates retain the existing EWMA policy: they are not
        converted into zero-count monitoring days.
        """
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in the range (0, 1]")
        if warmup_days <= 0:
            raise ValueError("warmup_days must be positive")

        now_utc = _as_utc(now)
        today = now_utc.astimezone(DISPLAY_TIMEZONE).date()
        observed = self.daily_cough_counts(client_id=client_id, now=now_utc)
        if not observed:
            return {
                "client_id": client_id,
                "start_date": None,
                "today": today.isoformat(),
                "alpha": alpha,
                "warmup_days": warmup_days,
                "completed_usable_days": 0,
                "warmup_remaining": warmup_days,
                "current_baseline": None,
                "_current_baseline_raw": None,
                "days": [],
            }

        start_date = date.fromisoformat(observed[0]["date"])
        days: list[dict] = [
            {
                "date": observed[0]["date"],
                "count": int(observed[0]["count"]),
                "complete": start_date < today,
                "usable": False,
                "phase": "start_partial",
                "baseline_before": None,
                "baseline_after": None,
                "display_baseline": None,
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
            display_baseline = None
            if index == warmup_days - 1:
                # The baseline becomes ready at the end of the seventh usable day.
                display_baseline = ewma
            elif index >= warmup_days:
                # Later days are assessed against the pre-update snapshot.
                display_baseline = baseline_before
            days.append(
                {
                    "date": item["date"],
                    "count": count,
                    "complete": True,
                    "usable": True,
                    "phase": "warmup" if index < warmup_days else "monitoring",
                    "baseline_before": _rounded(baseline_before),
                    "baseline_after": _rounded(ewma),
                    "display_baseline": _rounded(display_baseline),
                }
            )

        current_item = next(
            (
                item
                for item in observed[1:]
                if date.fromisoformat(item["date"]) == today
            ),
            None,
        )
        if current_item is not None:
            ready = len(completed) >= warmup_days
            days.append(
                {
                    "date": current_item["date"],
                    "count": int(current_item["count"]),
                    "complete": False,
                    "usable": False,
                    "phase": "current" if ready else "warmup_current",
                    "baseline_before": _rounded(ewma) if ready else None,
                    "baseline_after": None,
                    "display_baseline": _rounded(ewma) if ready else None,
                }
            )

        completed_count = len(completed)
        return {
            "client_id": client_id,
            "start_date": start_date.isoformat(),
            "today": today.isoformat(),
            "alpha": alpha,
            "warmup_days": warmup_days,
            "completed_usable_days": completed_count,
            "warmup_remaining": max(warmup_days - completed_count, 0),
            "current_baseline": (
                _rounded(ewma) if completed_count >= warmup_days else None
            ),
            "_current_baseline_raw": (
                ewma if completed_count >= warmup_days else None
            ),
            "days": sorted(days, key=lambda item: item["date"]),
        }

    def monitoring_progress_status(
        self,
        client_id: str,
        alpha: float = 0.2,
        warmup_days: int = 7,
        now: datetime | None = None,
        _timeline: dict | None = None,
    ) -> dict:
        """Summarize completed usable days into seven-day visual periods."""
        timeline = _timeline or self.personal_ewma_timeline(
            client_id, alpha=alpha, warmup_days=warmup_days, now=now
        )
        usable_days = [
            item
            for item in timeline["days"]
            if item.get("complete") and item.get("usable")
        ]
        weeks: list[dict] = []
        for offset in range(0, len(usable_days), 7):
            group = usable_days[offset : offset + 7]
            week_number = offset // 7 + 1
            average = sum(item["count"] for item in group) / len(group)
            ewma_reference = (
                group[0].get("baseline_before") if week_number > 1 else None
            )
            change_percent = (
                ((average - ewma_reference) / ewma_reference) * 100.0
                if ewma_reference is not None and ewma_reference > 0
                else None
            )
            weeks.append(
                {
                    "week": week_number,
                    "label": f"Week {week_number}",
                    "start_date": group[0]["date"],
                    "end_date": group[-1]["date"],
                    "usable_days": len(group),
                    "complete": len(group) == 7,
                    "average_per_day": round(average, 1),
                    "ewma_reference": _rounded(ewma_reference),
                    "change_percent": (
                        round(change_percent, 1)
                        if change_percent is not None
                        else None
                    ),
                    "status": (
                        "baseline_formation"
                        if week_number == 1
                        else "complete" if len(group) == 7 else "partial"
                    ),
                }
            )

        return {
            "client_id": client_id,
            "start_date": timeline["start_date"],
            "warmup_days": warmup_days,
            "completed_usable_days": timeline["completed_usable_days"],
            "warmup_remaining": timeline["warmup_remaining"],
            "weeks": weeks,
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
        _timeline: dict | None = None,
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
        timeline = _timeline or self.personal_ewma_timeline(
            client_id, alpha=alpha, warmup_days=warmup_days, now=now_utc
        )
        today = timeline["today"]
        today_item = next(
            (item for item in timeline["days"] if item["date"] == today),
            None,
        )
        today_count = int(today_item["count"]) if today_item is not None else None
        completed_count = int(timeline["completed_usable_days"])

        base = {
            "client_id": client_id,
            "today": today,
            "today_count": today_count,
            "current_available": today_count is not None,
            "monitoring_start_date": timeline["start_date"],
            "observed_history_days": completed_count,
            "warmup_days": warmup_days,
            "warmup_remaining": timeline["warmup_remaining"],
            "alpha": alpha,
            "threshold_pct": threshold_pct,
            "min_buffer": min_buffer,
        }

        if completed_count < warmup_days:
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

        baseline = float(timeline["_current_baseline_raw"])

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
