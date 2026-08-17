"""Polling callbacks for the doctor-facing dashboard."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dash
import plotly.graph_objects as go
import requests
from dash import Input, Output, State, html

from . import layout as L

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8050"))
API_BASE = f"http://127.0.0.1:{GATEWAY_PORT}/api"
try:
    DISPLAY_TIMEZONE = ZoneInfo(
        os.environ.get("GATEWAY_TIMEZONE", "Asia/Ho_Chi_Minh")
    )
except ZoneInfoNotFoundError:
    DISPLAY_TIMEZONE = ZoneInfo("UTC")

_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_INK_2 = "#52514e"
_MUTED = "#898781"
_GRID = "#e1e0d9"
_BASELINE = "#b46b2a"
_SERIES = "#2a78d6"
_TYPE_COLORS = {"dry": "#2a78d6", "wet": "#1baf7a", "unknown": "#898781"}
_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def api_get(path: str):
    try:
        response = requests.get(f"{API_BASE}{path}", timeout=2)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def register(app: dash.Dash) -> None:
    @app.callback(
        Output(L.CLIENT_DROPDOWN_ID, "options"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        State(L.CLIENT_DROPDOWN_ID, "options"),
    )
    def update_client_options(_n, current_options):
        clients = api_get("/clients")
        if clients is None:
            return dash.no_update
        options = [
            {"label": client["client_id"], "value": client["client_id"]}
            for client in clients
            if _visible_patient(client.get("client_id"))
        ]
        return dash.no_update if options == current_options else options

    @app.callback(
        Output(L.FLEET_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
    )
    def update_device_list(_n):
        devices = api_get("/devices")
        if devices is None:
            return dash.no_update
        return [
            {
                "device": device.get("device_id", "—"),
                "status": str(device.get("status") or "unknown").title(),
                "patient": device.get("client_id") or "Unassigned",
                "temperature": _fmt_number(device.get("temperature_c"), " °C"),
                "humidity": _fmt_number(device.get("humidity_percent"), " %"),
                "last_seen": _fmt_ts(device.get("last_seen")),
            }
            for device in devices
            if _visible_device(device)
        ]

    @app.callback(
        Output(L.COUNT_CHART_ID, "figure"),
        Output(L.TYPE_PIE_ID, "figure"),
        Output(L.DAY_COUNT_ID, "children"),
        Output(L.NIGHT_COUNT_ID, "children"),
        Output(L.DAY_CARD_ID, "title"),
        Output(L.NIGHT_CARD_ID, "title"),
        Output(L.TODAY_COUNT_ID, "children"),
        Output(L.LAST_EVENT_ID, "children"),
        Output(L.STATISTICAL_FINDING_ID, "children"),
        Output(L.LIVE_FEED_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.RANGE_TOGGLE_ID, "value"),
        Input(L.LIVE_FEED_DATE_ID, "date"),
    )
    def update_patient(_n, client_id, range_mode, event_date):
        if not client_id:
            empty = _placeholder_figure("Select a patient above")
            return (
                empty,
                empty,
                "—",
                "—",
                "Trend unavailable",
                "Trend unavailable",
                "—",
                "—",
                _empty_finding(),
                [],
            )

        stats = api_get(f"/clients/{quote(str(client_id), safe='')}/stats")
        events = api_get(_event_query(str(client_id), event_date))
        if stats is None or events is None:
            return (dash.no_update,) * 10

        is_7d = range_mode == "7d"
        day_night = stats.get("day_night_7d" if is_7d else "day_night_24h", {})
        trends = stats.get("day_night_trends", {})
        today_count = stats.get("today_count")
        return (
            _count_figure(stats, range_mode, str(client_id)),
            _type_figure(stats, range_mode),
            str(day_night.get("day", 0)),
            str(day_night.get("night", 0)),
            _trend_title("Day", trends.get("day", {})),
            _trend_title("Night", trends.get("night", {})),
            str(today_count) if today_count is not None else "Unavailable",
            _fmt_ts(stats.get("last_event_ts")),
            _weekly_finding_content(stats.get("weekly_finding", {})),
            [
                {
                    "event_time": _fmt_ts(event.get("event_ts")),
                    "event": _event_label(event),
                }
                for event in events
            ],
        )


def _event_query(client_id: str, event_date: str | None) -> str:
    path = f"/clients/{quote(client_id, safe='')}/events"
    params = {"order": "event"}
    if event_date:
        try:
            local_day = date.fromisoformat(str(event_date))
            start = datetime.combine(local_day, time.min, DISPLAY_TIMEZONE)
            end = start + timedelta(days=1) - timedelta(microseconds=1)
            params["from"] = _utc_iso(start)
            params["to"] = _utc_iso(end)
        except (TypeError, ValueError):
            pass
    return f"{path}?{urlencode(params)}"


def _count_figure(stats: dict, range_mode: str, client_id: str) -> go.Figure:
    fig = go.Figure()
    if range_mode == "7d":
        points = stats.get("per_day", [])
        x = [point.get("date") for point in points]
        y = [point.get("count", 0) for point in points]
        fig.add_bar(
            x=x,
            y=y,
            marker_color=_SERIES,
            marker_line_width=0,
            opacity=0.76,
            name="Daily bouts",
            hovertemplate="%{x|%m-%d}<br>%{y} bouts<extra></extra>",
        )
        baseline = stats.get("baseline", {}).get("baseline")
        if baseline is not None and x:
            anchor = _parse_datetime(stats.get("analysis_anchor_ts"))
            line_x = (
                [
                    (anchor - timedelta(days=6)).date().isoformat(),
                    anchor.date().isoformat(),
                ]
                if anchor is not None
                else [x[0], x[-1]]
            )
            fig.add_trace(
                go.Scatter(
                    x=line_x,
                    y=[baseline, baseline],
                    mode="lines",
                    line={"color": _BASELINE, "width": 2.2},
                    name="Personal EWMA",
                    hovertemplate="Personal EWMA: %{y:.1f} bouts/day<extra></extra>",
                )
            )
        _apply_chrome(fig, "Cough bouts — last 7 days")
        fig.update_layout(
            bargap=0.35,
            showlegend=baseline is not None,
            legend={"orientation": "h", "y": -0.18},
            dragmode=False,
            xaxis={"type": "date", "tickformat": "%m-%d", "fixedrange": True},
            uirevision=f"{client_id}:7d:{stats.get('analysis_anchor_ts', '')}",
        )
    else:
        points = stats.get("per_30_minute_history", stats.get("per_30_minute", []))
        x = [_parse_datetime(point.get("ts")) for point in points]
        labels = []
        for parsed in x:
            labels.append(
                f"{parsed:%H:%M}–{parsed + timedelta(minutes=30):%H:%M}"
                if parsed is not None
                else "30-minute interval"
            )
        fig.add_bar(
            x=x,
            y=[point.get("count", 0) for point in points],
            customdata=labels,
            marker_color=_SERIES,
            marker_line_width=0,
            opacity=0.76,
            name="Bout count",
            width=30 * 60 * 1000,
            offset=0,
            hovertemplate="%{customdata}<br>%{y} bouts<extra></extra>",
        )
        anchor = _parse_datetime(stats.get("analysis_anchor_ts"))
        xaxis = {
            "type": "date",
            "tickformat": "%H:%M",
            "fixedrange": False,
        }
        if anchor is not None:
            xaxis.update(
                {
                    "range": [anchor - timedelta(hours=24), anchor],
                    "tick0": anchor.replace(minute=0, second=0, microsecond=0),
                    "dtick": 3 * 60 * 60 * 1000,
                }
            )
        _apply_chrome(fig, "Cough bouts — last 24 hours")
        fig.update_layout(
            bargap=0.14,
            showlegend=False,
            dragmode="pan",
            xaxis=xaxis,
            uirevision=f"{client_id}:24h:{stats.get('analysis_anchor_ts', '')}",
        )

    fig.update_yaxes(fixedrange=True, tickmode="linear", dtick=1, rangemode="tozero")
    return fig


def _type_figure(stats: dict, range_mode: str) -> go.Figure:
    key = "by_type_7d" if range_mode == "7d" else "by_type_24h"
    by_type = stats.get(key, {})
    names = [name for name in ("wet", "dry", "unknown") if by_type.get(name)]
    if not names:
        return _placeholder_figure("No cough-bout data in this range")
    fig = go.Figure(
        data=[
            go.Pie(
                labels=[name.title() for name in names],
                values=[by_type[name] for name in names],
                marker={
                    "colors": [_TYPE_COLORS[name] for name in names],
                    "line": {"color": _SURFACE, "width": 2},
                },
                hole=0.55,
                textinfo="label+percent",
                hovertemplate="%{label}: %{value} bouts<extra></extra>",
            )
        ]
    )
    _apply_chrome(fig, "Wet / Dry / Unknown")
    fig.update_layout(showlegend=True, legend={"orientation": "h", "y": -0.1})
    return fig


def _weekly_finding_content(status: dict):
    if not status or not status.get("start_date"):
        return _empty_finding()
    active_week = int(status.get("active_week", 1))
    completed = int(status.get("completed_days_in_week", 0))
    if active_week == 1:
        heading = "PERSONAL BASELINE FORMATION"
        progress = f"{completed} / 7 completed days"
    else:
        heading = f"TREATMENT WEEK {active_week}"
        progress = f"Calculating · {completed} / 7 completed days"

    children = [
        html.Div(
            className="week-current",
            children=[html.Strong(heading), html.Span(progress)],
        )
    ]
    latest = status.get("latest_completed_period")
    if latest:
        change = latest.get("change_percent")
        if change is None:
            change_text = "N/A"
        else:
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            change_text = f"{arrow}{abs(change):.1f}%"
        reference = latest.get("week_reference_snapshot")
        hover = (
            f"Start-of-week Personal EWMA: {reference:.1f} bouts/day"
            if reference is not None
            else "Start-of-week Personal EWMA was zero; percentage unavailable"
        )
        children.append(
            html.Div(
                className="week-latest",
                children=[
                    html.Span("LAST COMPLETED PERIOD", className="field-label"),
                    html.Strong(f"Week {latest.get('week')}", className="week-number"),
                    html.Strong(
                        f"{latest.get('weekly_level', 0):.1f} bouts/day",
                        className="week-level",
                    ),
                    html.Strong(change_text, className="week-change", title=hover),
                ],
            )
        )
    return html.Div(className="week-finding", children=children)


def _empty_finding():
    return html.P(
        "Select a patient to evaluate monitoring progress.", className="empty-state"
    )


def _trend_title(label: str, status: dict) -> str:
    trend = status.get("trend") if status else None
    if trend:
        return f"Latest completed {label.lower()} period: {trend.title()}"
    reason = status.get("reason") if status else None
    if reason == "active_period":
        return f"{label} trend will be available after the active period completes"
    if reason == "warmup":
        return f"{label} trend is forming its 7-period baseline"
    return f"{label} trend unavailable"


def _event_label(event: dict) -> str:
    cough_type = str(event.get("cough_type") or "unknown").title()
    duration = event.get("duration_s")
    duration_text = ""
    if duration is not None:
        try:
            duration_text = f" · {int(duration)} s"
        except (TypeError, ValueError):
            pass
    label = "Prolonged bout" if event.get("prolonged") else "Cough bout"
    return f"{cough_type} · {label}{duration_text}"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TIMEZONE).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _visible_patient(client_id) -> bool:
    normalized = str(client_id or "").strip().lower()
    return normalized not in {"", "unknown", "unassigned", "client_test_alert"}


def _visible_device(device: dict) -> bool:
    device_id = str(device.get("device_id") or "").strip().lower()
    return device_id != "device_test_alert" and _visible_patient(device.get("client_id"))


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def _fmt_number(value, suffix: str) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _placeholder_figure(message: str) -> go.Figure:
    fig = go.Figure()
    _apply_chrome(fig, "")
    fig.update_layout(
        xaxis={"visible": False},
        yaxis={"visible": False},
        annotations=[
            {
                "text": message,
                "showarrow": False,
                "font": {"size": 13, "color": _MUTED},
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": 0.5,
            }
        ],
    )
    return fig


def _apply_chrome(fig: go.Figure, title: str) -> None:
    fig.update_layout(
        title={"text": title, "font": {"size": 14, "color": _INK}},
        height=310,
        margin={"l": 44, "r": 16, "t": 48, "b": 42},
        paper_bgcolor=_SURFACE,
        plot_bgcolor=_SURFACE,
        font={"family": _FONT, "size": 12, "color": _INK_2},
        xaxis={"gridcolor": _GRID, "linecolor": "#c3c2b7", "zeroline": False},
        yaxis={"gridcolor": _GRID, "linecolor": "#c3c2b7", "zeroline": False},
    )
