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
        Output(L.PROGRESS_START_ID, "children"),
        Output(L.PROGRESS_CHART_ID, "figure"),
        Output(L.LIVE_FEED_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.RANGE_TOGGLE_ID, "value"),
    )
    def update_patient(_n, client_id, range_mode):
        if not client_id:
            empty = _placeholder_figure("Select a patient above")
            return (
                empty,
                empty,
                "—",
                "—",
                "—",
                "—",
                _empty_baseline(),
                "—",
                _placeholder_figure("Select a patient above"),
                [],
            )

        stats = api_get(f"/clients/{client_id}/stats")
        events = api_get(f"/clients/{client_id}/events?limit=50&order=event")
        if stats is None or events is None:
            return (dash.no_update,) * 10

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
            _fmt_date(stats.get("monitoring_progress", {}).get("start_date")),
            _progress_figure(stats.get("monitoring_progress", {})),
            [
                {
                    "event_time": _fmt_ts(event.get("event_ts")),
                    "event": _event_label(event),
                }
                for event in events
            ],
        )


def _count_figure(stats: dict, range_mode: str, client_id: str) -> go.Figure:
    fig = go.Figure()
    if range_mode == "7d":
        points = stats.get("per_day", [])
        x = [point.get("date") for point in points]
        y = [point.get("count", 0) for point in points]
        title = "Cough bouts — last 7 days"
        xaxis_options = {
            "type": "date",
            "tickformat": "%m-%d",
            "fixedrange": True,
        }
        dragmode = False
        fig.add_bar(
            x=x,
            y=y,
            marker_color=_SERIES,
            marker_line_width=0,
            opacity=0.72,
            name="Daily bouts",
            hovertemplate="%{x|%m-%d}: %{y} bouts<extra></extra>",
        )
        baseline_points = [
            point for point in points if point.get("ewma_baseline") is not None
        ]
        if baseline_points:
            fig.add_trace(
                go.Scatter(
                    x=[point["date"] for point in baseline_points],
                    y=[point["ewma_baseline"] for point in baseline_points],
                    mode="lines+markers",
                    line={"color": "#b46b2a", "width": 2.2},
                    marker={"size": 6},
                    name="Personal EWMA",
                    hovertemplate=(
                        "%{x|%m-%d}<br>Pre-update EWMA: %{y:.1f}"
                        "<extra></extra>"
                    ),
                )
            )
        elif points:
            baseline = stats.get("baseline", {})
            formed = min(
                int(baseline.get("observed_history_days", 0)),
                int(baseline.get("warmup_days", 7)),
            )
            fig.add_annotation(
                text=f"Baseline formation — Day {formed} / 7",
                xref="paper",
                yref="paper",
                x=0.01,
                y=0.98,
                showarrow=False,
                font={"size": 12, "color": _MUTED},
                bgcolor="rgba(252,252,251,0.88)",
            )
    else:
        points = stats.get(
            "per_30_minute_history",
            stats.get("per_30_minute", []),
        )
        x = [_parse_datetime(point.get("ts")) for point in points]
        title = "Cough bouts — last 24 hours"
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
        customdata = []
        for point, parsed in zip(points, x):
            end = parsed + timedelta(minutes=30) if parsed is not None else None
            label = (
                f"{parsed:%H:%M}–{end:%H:%M}"
                if parsed is not None and end is not None
                else "30-minute interval"
            )
            customdata.append(
                [
                    label,
                    point.get("total", 0),
                    point.get("wet", 0),
                    point.get("dry", 0),
                    point.get("unknown", 0),
                ]
            )
        hover = (
            "%{customdata[0]}<br>Total: %{customdata[1]}"
            "<br>Wet: %{customdata[2]}<br>Dry: %{customdata[3]}"
            "<br>Unknown: %{customdata[4]}<extra></extra>"
        )
        for cough_type in ("wet", "dry", "unknown"):
            fig.add_bar(
                x=x,
                y=[point.get(cough_type, 0) for point in points],
                customdata=customdata,
                marker_color=_TYPE_COLORS[cough_type],
                marker_line_width=0,
                opacity=0.82,
                name=cough_type.title(),
                width=30 * 60 * 1000,
                offset=0,
                hovertemplate=hover,
            )

    _apply_chrome(fig, title)
    fig.update_layout(
        bargap=0.14,
        barmode="stack" if range_mode == "24h" else "group",
        showlegend=True,
        legend={"orientation": "h", "y": -0.18},
        dragmode=dragmode,
        # Preserve a manual pan while data is unchanged. A newly captured bout
        # changes the anchor and naturally returns the viewport to latest data.
        uirevision=(
            f"{client_id}:{range_mode}:{stats.get('analysis_anchor_ts', '')}"
        ),
        xaxis=xaxis_options,
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


def _baseline_content(status: dict):
    if not status:
        return _empty_baseline()
    if not status.get("available"):
        formed = min(
            int(status.get("observed_history_days", 0)),
            int(status.get("warmup_days", 7)),
        )
        return html.Div(
            className="finding finding-warmup",
            children=[
                html.Strong("Baseline formation"),
                html.Div(
                    className="warmup-progress",
                    children=[
                        html.Strong(f"Day {formed} / 7"),
                        html.Div(
                            className="warmup-track",
                            children=html.Div(
                                className="warmup-fill",
                                style={"width": f"{formed / 7 * 100:.1f}%"},
                            ),
                        ),
                    ],
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


def _progress_figure(progress: dict) -> go.Figure:
    weeks = progress.get("weeks", []) if progress else []
    if not weeks:
        return _placeholder_figure("Waiting for completed monitoring days")

    labels = [week.get("label", "Week") for week in weeks]
    values = [week.get("average_per_day", 0) for week in weeks]
    colors = []
    text = []
    customdata = []
    for week in weeks:
        if week.get("week") == 1:
            colors.append("#898781")
            status_text = (
                "Baseline formation"
                if week.get("complete")
                else "Baseline formation · Partial"
            )
        elif not week.get("complete"):
            colors.append("#9ebfe5")
            change = week.get("change_percent")
            status_text = (
                f"Partial · {change:+.1f}%" if change is not None else "Partial"
            )
        else:
            colors.append(_SERIES)
            change = week.get("change_percent")
            status_text = f"{change:+.1f}%" if change is not None else "—"
        text.append(status_text)
        customdata.append(
            [
                week.get("start_date", "—"),
                week.get("end_date", "—"),
                week.get("usable_days", 0),
                status_text,
            ]
        )

    fig = go.Figure(
        data=[
            go.Bar(
                x=labels,
                y=values,
                marker_color=colors,
                marker_line_width=0,
                text=text,
                textposition="outside",
                customdata=customdata,
                hovertemplate=(
                    "%{x}<br>%{customdata[0]} to %{customdata[1]}"
                    "<br>Average: %{y:.1f} bouts/day"
                    "<br>Usable days: %{customdata[2]}"
                    "<br>%{customdata[3]}<extra></extra>"
                ),
            )
        ]
    )
    _apply_chrome(fig, "Weekly cough-frequency summary")
    fig.update_layout(
        height=280,
        showlegend=False,
        bargap=0.42,
        xaxis={"fixedrange": True},
        yaxis={"fixedrange": True, "rangemode": "tozero", "dtick": 1},
    )
    return fig


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


def _fmt_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y")
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
