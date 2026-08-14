"""Polling callbacks for the doctor-facing dashboard."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
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
_BASELINE = "#c3c2b7"
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
        Output(L.TODAY_COUNT_ID, "children"),
        Output(L.LAST_EVENT_ID, "children"),
        Output(L.BASELINE_STATUS_ID, "children"),
        Output(L.LIVE_FEED_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.RANGE_TOGGLE_ID, "value"),
    )
    def update_patient(_n, client_id, range_mode):
        if not client_id:
            empty = _placeholder_figure("Select a patient above")
            return empty, empty, "—", "—", "—", "—", _empty_baseline(), []

        stats = api_get(f"/clients/{client_id}/stats")
        events = api_get(f"/clients/{client_id}/events?limit=50&order=event")
        if stats is None or events is None:
            return (dash.no_update,) * 8

        is_7d = range_mode == "7d"
        day_night = stats.get("day_night_7d" if is_7d else "day_night_24h", {})
        today_count = stats.get("today_count")
        return (
            _count_figure(stats, range_mode, client_id),
            _type_figure(stats, range_mode),
            str(day_night.get("day", 0)),
            str(day_night.get("night", 0)),
            str(today_count) if today_count is not None else "Unavailable",
            _fmt_ts(stats.get("last_event_ts")),
            _baseline_content(stats.get("baseline", {})),
            [
                {
                    "event_time": _fmt_ts(event.get("event_ts")),
                    "event": _event_label(event),
                }
                for event in events
            ],
        )


def _count_figure(stats: dict, range_mode: str, client_id: str) -> go.Figure:
    if range_mode == "7d":
        points = stats.get("per_day", [])
        x = [point.get("date") for point in points]
        y = [point.get("count", 0) for point in points]
        title = f"Cough-bout trend — last 7 days ({stats.get('last_7d_count', 0)})"
        xaxis_options = {
            "type": "date",
            "tickformat": "%m-%d",
            "fixedrange": True,
        }
        dragmode = False
        bar_options = {}
    else:
        points = stats.get(
            "per_10_minute_history",
            stats.get("per_10_minute", []),
        )
        x = [_parse_datetime(point.get("ts")) for point in points]
        y = [point.get("count", 0) for point in points]
        title = (
            "Cough-bout trend — 10-minute totals, last 24 hours "
            f"({stats.get('last_24h_count', 0)})"
        )
        anchor = _parse_datetime(stats.get("analysis_anchor_ts"))
        xaxis_options = {
            "type": "date",
            "tickformat": "%H:%M",
            "hoverformat": "%m-%d %H:%M",
            "fixedrange": False,
        }
        if anchor is not None:
            xaxis_options["range"] = [
                anchor - timedelta(hours=24),
                anchor,
            ]
            # Align ticks to the patient's local clock and always include the
            # hour containing the newest cough at the right edge.
            xaxis_options["tick0"] = anchor.replace(
                minute=0, second=0, microsecond=0
            )
            xaxis_options["dtick"] = 3 * 60 * 60 * 1000
        dragmode = "pan"
        # Plotly date widths are milliseconds. Offset zero makes each bar span
        # from its bucket start through the following ten minutes.
        bar_options = {"width": 10 * 60 * 1000, "offset": 0}

    fig = go.Figure()
    fig.add_bar(
        x=x,
        y=y,
        marker_color=_SERIES,
        marker_line_width=0,
        opacity=0.72,
        name="Bout count",
        hovertemplate="%{x|%m-%d %H:%M}: %{y} bouts<extra></extra>",
        **bar_options,
    )
    _apply_chrome(fig, title)
    fig.update_layout(
        bargap=0.32,
        showlegend=False,
        dragmode=dragmode,
        # Preserve a manual pan while data is unchanged. A newly captured bout
        # changes the anchor and naturally returns the viewport to latest data.
        uirevision=(
            f"{client_id}:{range_mode}:{stats.get('analysis_anchor_ts', '')}"
        ),
        xaxis=xaxis_options,
    )
    fig.update_yaxes(fixedrange=True)
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


def _baseline_content(status: dict):
    if not status:
        return _empty_baseline()
    if not status.get("available"):
        remaining = status.get("warmup_remaining", 7)
        return html.Div(
            className="finding finding-warmup",
            children=[
                html.Strong("Establishing recent personal baseline"),
                html.P(
                    f"{remaining} more completed observed day(s) required.",
                    className="finding-copy",
                ),
            ],
        )
    if not status.get("current_available"):
        return html.Div(
            className="finding finding-neutral",
            children=[
                html.Strong("Today's bout count is unavailable"),
                html.P(
                    f"EWMA baseline: {status['baseline']:.1f} bouts/day",
                    className="finding-copy",
                ),
            ],
        )

    change = status.get("change_percent")
    change_text = f"{change:+.1f}%" if change is not None else "—"
    title = (
        "Above recent personal bout baseline"
        if status.get("above_baseline")
        else "Within recent personal bout baseline"
    )
    return html.Div(
        className=(
            "finding finding-active"
            if status.get("above_baseline")
            else "finding finding-stable"
        ),
        children=[
            html.Strong(title),
            html.Div(
                className="finding-values",
                children=[
                    _finding_value("Current", status.get("today_count")),
                    _finding_value("EWMA baseline", f"{status['baseline']:.1f}"),
                    _finding_value("Change", change_text),
                ],
            ),
        ],
    )


def _finding_value(label: str, value) -> html.Div:
    return html.Div(children=[html.Span(label), html.Strong(str(value))])


def _empty_baseline():
    return html.P(
        "Select a patient to evaluate the recent personal baseline.",
        className="empty-state",
    )


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


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        # Plotly serializes aware datetimes to UTC. Supplying a naive datetime
        # after conversion keeps axis labels in the configured patient-facing
        # timezone instead of shifting Vietnam time seven hours backwards.
        return parsed.astimezone(DISPLAY_TIMEZONE).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _visible_patient(client_id) -> bool:
    normalized = str(client_id or "").strip().lower()
    return normalized not in {"", "unknown", "unassigned", "client_test_alert"}


def _visible_device(device: dict) -> bool:
    device_id = str(device.get("device_id") or "").strip().lower()
    return device_id != "device_test_alert" and _visible_patient(
        device.get("client_id")
    )


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
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
        xaxis={"gridcolor": _GRID, "linecolor": _BASELINE, "zeroline": False},
        yaxis={"gridcolor": _GRID, "linecolor": _BASELINE, "zeroline": False},
    )
