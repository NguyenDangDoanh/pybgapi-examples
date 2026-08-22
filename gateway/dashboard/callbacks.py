"""Polling callbacks for the doctor-facing dashboard."""

from __future__ import annotations

import math
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
_AXIS = "#c3c2b7"
_TYPE_COLORS = {"dry": "#5f82ad", "wet": "#679b82", "unknown": "#9a948c"}
_PERIOD_COLORS = {"day": "#6e91bd", "night": "#6f7798"}
_BASELINE_COLOR = "#7d8896"
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
        Output(L.COUGH_EVENT_DATE_ID, "date"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
    )
    def reset_event_date(_client_id):
        return None

    @app.callback(
        Output(L.LIVE_FEED_TABLE_ID, "page_current"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.COUGH_EVENT_DATE_ID, "date"),
    )
    def reset_event_page(_client_id, _selected_date):
        return 0

    @app.callback(
        Output(L.COUNT_CHART_ID, "figure"),
        Output(L.TODAY_COUNT_ID, "children"),
        Output(L.TOTAL_LABEL_ID, "children"),
        Output(L.LAST_EVENT_ID, "children"),
        Output(L.BASELINE_STATUS_ID, "children"),
        Output(L.TREATMENT_RESPONSE_ID, "children"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.RANGE_TOGGLE_ID, "value"),
    )
    def update_patient(_n, client_id, range_mode):
        is_7d = range_mode == "7d"
        total_label = "Cough bouts — last 7 completed days" if is_7d else "Cough bouts — last 24 hours"
        if not client_id:
            return (
                _placeholder_figure("Select a patient above"),
                "—",
                total_label,
                "—",
                _empty_baseline(),
                _empty_treatment(),
            )

        stats = api_get(f"/clients/{quote(str(client_id), safe='')}/stats")
        if stats is None:
            return (dash.no_update,) * 6

        total_key = "last_7d_count" if is_7d else "last_24h_count"
        total = stats.get(total_key)
        return (
            _count_figure(stats, range_mode, str(client_id)),
            str(total) if total is not None else "Unavailable",
            total_label,
            _fmt_ts(stats.get("last_event_ts")),
            _baseline_content(stats.get("baseline", {})),
            _treatment_content(stats.get("treatment_response", {})),
        )

    @app.callback(
        Output(L.LIVE_FEED_TABLE_ID, "data"),
        Output(L.LIVE_FEED_TABLE_ID, "page_count"),
        Output(L.COUGH_EVENT_EMPTY_ID, "children"),
        Output(L.COUGH_EVENT_EMPTY_ID, "style"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.COUGH_EVENT_DATE_ID, "date"),
        Input(L.LIVE_FEED_TABLE_ID, "page_current"),
        State(L.LIVE_FEED_TABLE_ID, "page_size"),
    )
    def update_events(_n, client_id, selected_date, page_current, page_size):
        page_size = max(int(page_size or 25), 1)
        page = max(int(page_current or 0), 0) + 1
        if not client_id:
            return [], 1, "Select a patient to view cough events.", {"display": "block"}

        params: dict[str, str | int] = {"page": page, "page_size": page_size}
        if selected_date:
            params.update(_date_range_params(str(selected_date)))
        payload = api_get(
            f"/clients/{quote(str(client_id), safe='')}/events/page?{urlencode(params)}"
        )
        if payload is None:
            return (dash.no_update,) * 4

        events = payload.get("items", [])
        rows = [
            {
                "time": _fmt_event_ts(
                    event.get("event_ts") or event.get("received_ts"),
                    include_date=not bool(selected_date),
                ),
                "type": str(event.get("cough_type") or "unknown").title(),
                "device": event.get("device_id") or "—",
            }
            for event in events
        ]
        total = int(payload.get("total") or 0)
        page_count = max(math.ceil(total / page_size), 1)
        if rows:
            return rows, page_count, "", {"display": "none"}
        message = (
            "No cough events were recorded on this date."
            if selected_date
            else "No cough events are available for this patient."
        )
        return [], page_count, message, {"display": "block"}


def _count_figure(stats: dict, range_mode: str, client_id: str) -> go.Figure:
    if range_mode == "7d":
        return _seven_day_figure(stats, client_id)
    return _rolling_24h_figure(stats, client_id)


def _rolling_24h_figure(stats: dict, client_id: str) -> go.Figure:
    points = stats.get("per_30_minute", [])
    window_start = _parse_datetime(stats.get("window_24h_start"))
    window_end = _parse_datetime(stats.get("window_24h_end"))
    x_values = []
    custom = []
    totals = []
    valid_points = []
    for point in points:
        start = _parse_datetime(point.get("ts"))
        if start is None:
            continue
        end = start + timedelta(minutes=30)
        display_end = end - timedelta(seconds=1)
        dry = int(point.get("dry", 0))
        wet = int(point.get("wet", 0))
        unknown = int(point.get("unknown", 0))
        total = int(point.get("total", dry + wet + unknown))
        valid_points.append(point)
        x_values.append(start + timedelta(minutes=15))
        totals.append(total)
        custom.append(
            [
                f"{start:%m-%d %H:%M}–{display_end:%H:%M}",
                total,
                dry,
                wet,
                unknown,
            ]
        )

    fig = go.Figure()
    for cough_type in ("dry", "wet", "unknown"):
        fig.add_bar(
            x=x_values,
            y=[int(point.get(cough_type, 0)) for point in valid_points],
            width=28 * 60 * 1000,
            name=cough_type.title(),
            marker_color=_TYPE_COLORS[cough_type],
            marker_line_width=0,
            hoverinfo="skip",
        )
    fig.add_scatter(
        x=x_values,
        y=totals,
        mode="markers",
        marker={"size": 16, "opacity": 0},
        showlegend=False,
        customdata=custom,
        hovertemplate=(
            "%{customdata[0]}<br>"
            "Total: %{customdata[1]}<br>"
            "Dry: %{customdata[2]}<br>"
            "Wet: %{customdata[3]}<br>"
            "Unknown: %{customdata[4]}<extra></extra>"
        ),
    )
    _apply_chrome(fig, f"Cough bouts — rolling 24 hours ({sum(totals)})")
    xaxis = {
        "type": "date",
        "tickformat": "%H:%M",
        "hoverformat": "%m-%d %H:%M",
        "fixedrange": True,
        "dtick": 3 * 60 * 60 * 1000,
    }
    if window_start is not None and window_end is not None:
        xaxis["range"] = [window_start, window_end]
        xaxis["tick0"] = window_end.replace(minute=0, second=0, microsecond=0)
    fig.update_layout(
        barmode="stack",
        bargap=0.06,
        dragmode=False,
        showlegend=True,
        legend={"orientation": "h", "y": -0.19, "x": 0},
        uirevision=f"{client_id}:24h:{stats.get('window_24h_end', '')}",
        xaxis=xaxis,
    )
    fig.update_yaxes(fixedrange=True, rangemode="tozero", dtick=1)
    return fig


def _seven_day_figure(stats: dict, client_id: str) -> go.Figure:
    points = stats.get("per_day", [])
    if not stats.get("completed_7d_available") or len(points) != 7:
        return _placeholder_figure("Not enough completed days to show a 7-day trend")

    x_values = [point.get("date") for point in points]
    custom = [
        [
            point.get("date"),
            int(point.get("total", 0)),
            int(point.get("day", 0)),
            int(point.get("night", 0)),
            int(point.get("dry", 0)),
            int(point.get("wet", 0)),
            int(point.get("unknown", 0)),
        ]
        for point in points
    ]
    fig = go.Figure()
    for period in ("day", "night"):
        fig.add_bar(
            x=x_values,
            y=[int(point.get(period, 0)) for point in points],
            name=period.title(),
            marker_color=_PERIOD_COLORS[period],
            marker_line_width=0,
            hoverinfo="skip",
        )

    fig.add_scatter(
        x=x_values,
        y=[int(point.get("total", 0)) for point in points],
        mode="markers",
        marker={"size": 18, "opacity": 0},
        showlegend=False,
        customdata=custom,
        hovertemplate=(
            "Date: %{customdata[0]}<br>"
            "Total: %{customdata[1]}<br>"
            "Day: %{customdata[2]}<br>"
            "Night: %{customdata[3]}<br>"
            "Dry: %{customdata[4]}<br>"
            "Wet: %{customdata[5]}<br>"
            "Unknown: %{customdata[6]}<extra></extra>"
        ),
    )

    baseline = stats.get("baseline", {})
    if baseline.get("available") and baseline.get("baseline") is not None:
        value = float(baseline["baseline"])
        updated = baseline.get("updated_through") or "—"
        fig.add_scatter(
            x=[x_values[0], x_values[-1]],
            y=[value, value],
            mode="lines",
            name="Personal baseline",
            showlegend=False,
            line={"color": _BASELINE_COLOR, "width": 2, "dash": "dash"},
            customdata=[[updated], [updated]],
            hovertemplate=(
                "Personal baseline: %{y:.1f} bouts/day<br>"
                "Updated through: %{customdata[0]}<extra></extra>"
            ),
        )
        fig.add_annotation(
            x=x_values[-1],
            y=value,
            text="Personal baseline",
            showarrow=False,
            xanchor="right",
            yanchor="bottom",
            font={"size": 11, "color": _BASELINE_COLOR},
        )

    total = sum(int(point.get("total", 0)) for point in points)
    _apply_chrome(fig, f"Cough bouts — 7 completed days ({total})")
    fig.update_layout(
        barmode="stack",
        bargap=0.28,
        dragmode=False,
        showlegend=True,
        legend={"orientation": "h", "y": -0.19, "x": 0},
        uirevision=f"{client_id}:7d:{stats.get('window_7d_end', '')}",
        xaxis={"type": "date", "tickformat": "%b %d", "fixedrange": True},
    )
    fig.update_yaxes(fixedrange=True, rangemode="tozero", dtick=1)
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


def _treatment_content(status: dict):
    if not status or not status.get("first_data_date"):
        return _empty_treatment()
    current_week = status.get("current_week_number") or 1
    current_range = _format_date_range(
        status.get("current_week_start"), status.get("current_week_end")
    )
    if not status.get("available"):
        reason = status.get("reason")
        if reason == "warmup":
            title = "Building personal treatment baseline"
            message = (
                f"Week {current_week} is in progress ({current_range}). "
                f"{status.get('warmup_remaining', 7)} completed day(s) remain."
            )
        elif reason == "awaiting_completed_comparison_week":
            title = "Personal baseline established"
            message = (
                f"Week {current_week} is in progress ({current_range}). "
                "The response comparison appears after this week is complete."
            )
        else:
            title = "Treatment response is not available yet"
            message = "No valid patient event data is available."
        return html.Div(
            className="finding finding-warmup",
            children=[html.Strong(title), html.P(message, className="finding-copy")],
        )

    direction = status.get("direction")
    evaluation_week = status.get("evaluation_week_number")
    title = {
        "decreased": f"Week {evaluation_week}: fewer cough bouts",
        "increased": f"Week {evaluation_week}: more cough bouts",
        "unchanged": f"Week {evaluation_week}: cough bouts unchanged",
    }.get(direction, f"Week {evaluation_week} response")
    change = status.get("change_percent")
    change_text = f"{change:+.1f}%" if change is not None else "Unavailable"
    return html.Div(
        className="finding finding-treatment",
        children=[
            html.Strong(title),
            html.Div(
                className="finding-values treatment-values",
                children=[
                    _finding_value("Personal baseline", f"{status['baseline']:.1f}/day"),
                    _finding_value(f"Week {evaluation_week}", f"{status['current']:.1f}/day"),
                    _finding_value("Change", change_text),
                ],
            ),
            html.P(
                f"Week {current_week} is in progress ({current_range}). "
                "This comparison is descriptive and does not attribute change to treatment.",
                className="finding-copy",
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


def _empty_treatment():
    return html.P(
        "Select a patient to evaluate automatic treatment-week trends.",
        className="empty-state",
    )


def _date_range_params(value: str) -> dict[str, str]:
    selected = date.fromisoformat(value[:10])
    start = datetime.combine(selected, time.min, tzinfo=DISPLAY_TIMEZONE)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return {"from": _iso_utc(start), "to": _iso_utc(end)}


def _format_date_range(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "date unavailable"
    try:
        return f"{date.fromisoformat(start):%b %d}–{date.fromisoformat(end):%b %d}"
    except ValueError:
        return f"{start}–{end}"


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
    return device_id != "device_test_alert" and _visible_patient(
        device.get("client_id")
    )


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


def _fmt_event_ts(value: str | None, include_date: bool) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S" if include_date else "%H:%M:%S"
        return parsed.astimezone(DISPLAY_TIMEZONE).strftime(fmt)
    except (TypeError, ValueError):
        return str(value)


def _fmt_number(value, suffix: str) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _placeholder_figure(message: str) -> go.Figure:
    fig = go.Figure()
    _apply_chrome(fig, "")
    fig.update_layout(
        xaxis={"visible": False, "fixedrange": True},
        yaxis={"visible": False, "fixedrange": True},
        dragmode=False,
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
        height=330,
        margin={"l": 44, "r": 16, "t": 48, "b": 54},
        paper_bgcolor=_SURFACE,
        plot_bgcolor=_SURFACE,
        font={"family": _FONT, "size": 12, "color": _INK_2},
        hovermode="x unified",
        xaxis={"gridcolor": _GRID, "linecolor": _AXIS, "zeroline": False},
        yaxis={"gridcolor": _GRID, "linecolor": _AXIS, "zeroline": False},
    )
