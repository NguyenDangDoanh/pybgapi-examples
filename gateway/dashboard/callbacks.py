"""Interval-driven callbacks.

All data access goes through api_get() -> the gateway REST API on localhost, so
the API contract (a [Must]) stays exercised even though dashboard and gateway
share one process. On API timeout/error every callback returns dash.no_update:
the page keeps its last good data instead of blanking.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dash
import plotly.express as px
import plotly.graph_objects as go
import requests
from dash import Input, Output, State, html

from . import layout as L

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8050"))
API_BASE = f"http://127.0.0.1:{GATEWAY_PORT}/api"
TIMEZONE_NAME = os.environ.get("GATEWAY_TIMEZONE", "Asia/Ho_Chi_Minh")
try:
    DISPLAY_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    DISPLAY_TIMEZONE = ZoneInfo("UTC")

# Chart tokens (light surface). Type colors follow the entity, never its rank:
# dry/wet are identities (categorical slots), unknown is deliberately muted.
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
    """GET /api{path} from the gateway; returns parsed JSON or None on failure."""
    try:
        resp = requests.get(f"{API_BASE}{path}", timeout=2)
        resp.raise_for_status()
        return resp.json()
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
            {"label": c["client_id"], "value": c["client_id"]} for c in clients
        ]
        # Rewriting the options prop closes an open dropdown menu, so only
        # push when the client list actually changed.
        if options == current_options:
            return dash.no_update
        return options

    @app.callback(
        Output(L.FLEET_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
    )
    def update_fleet(_n):
        devices = api_get("/devices")
        if devices is None:
            return dash.no_update
        return [
            {
                "device": d["device_id"],
                "status": d.get("status", "unknown"),
                "client": d.get("client_id") or "unassigned",
                "last_seen": _fmt_ts(d.get("last_seen")),
                "temperature": _fmt_number(d.get("temperature_c"), " °C"),
                "humidity": _fmt_number(d.get("humidity_percent"), " %"),
            }
            for d in devices
        ]

    @app.callback(
        Output(L.LIVE_FEED_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
    )
    def update_live_feed(_n):
        events = api_get("/events/recent")
        if events is None:
            return dash.no_update
        return [
            {
                "time": _fmt_ts(e.get("received_ts")),
                "device": e["device_id"],
                "client": e.get("client_id") or "unassigned",
                "type": e.get("cough_type", "unknown"),
            }
            for e in events
        ]

    @app.callback(
        Output(L.COUNT_CHART_ID, "figure"),
        Output(L.TYPE_PIE_ID, "figure"),
        Output(L.CLIENT_EVENTS_TABLE_ID, "data"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
        Input(L.RANGE_TOGGLE_ID, "value"),
    )
    def update_client_detail(_n, client_id, range_mode):
        if not client_id:
            # Placeholder depends only on the (absent) selection: skip interval
            # ticks so the page isn't re-rendered while the user is picking a
            # client from the open dropdown menu.
            if dash.ctx.triggered_id == L.POLL_INTERVAL_ID:
                return dash.no_update, dash.no_update, dash.no_update
            empty = _placeholder_figure("Select a client above")
            return empty, empty, []
        stats = api_get(f"/clients/{client_id}/stats")
        events = api_get(f"/clients/{client_id}/events")
        if stats is None or events is None:
            return dash.no_update, dash.no_update, dash.no_update
        return (
            _count_figure(stats, range_mode),
            _type_figure(stats),
            [
                {
                    # event_ts may be absent (device unsynced) -> received_ts,
                    # the [Must] fallback from project_info.md.
                    "time": _fmt_ts(e.get("event_ts") or e.get("received_ts")),
                    "device": e["device_id"],
                    "type": e.get("cough_type", "unknown"),
                }
                for e in events
            ],
        )

    @app.callback(
        Output(L.BASELINE_ALERTS_ID, "children"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
    )
    def update_baseline_alerts(_n, client_id):
        """Show only EWMA baseline alerts without changing suggestions."""
        if not client_id:
            if dash.ctx.triggered_id == L.POLL_INTERVAL_ID:
                return dash.no_update

            return html.P(
                "Select a client to evaluate the cough baseline.",
                className="baseline-alert-empty",
            )

        stats = api_get(f"/clients/{client_id}/stats")
        if stats is None:
            return dash.no_update

        baseline_alerts = [
            item
            for item in stats.get("suggestions", [])
            if item.get("rule") == "cough_above_ewma_baseline"
        ]

        if not baseline_alerts:
            return html.Div(
                className="baseline-alert-ok",
                children=[
                    html.Span("Stable", className="baseline-alert-ok-badge"),
                    html.Span(
                        "No EWMA baseline alert is currently active."
                    ),
                ],
            )

        return [
            html.Div(
                className="baseline-alert-active",
                children=[
                    html.Div(
                        className="baseline-alert-title",
                        children=[
                            html.Span(
                                "Alert",
                                className="baseline-alert-badge",
                            ),
                            html.Strong(
                                "Cough level above personal baseline"
                            ),
                        ],
                    ),
                    html.P(
                        item.get("text", "Baseline threshold exceeded."),
                        className="baseline-alert-message",
                    ),
                    html.P(
                        f"Client: {client_id}",
                        className="baseline-alert-client",
                    ),
                ],
            )
            for item in baseline_alerts
        ]

    @app.callback(
        Output(L.SUGGESTIONS_LIST_ID, "children"),
        Input(L.POLL_INTERVAL_ID, "n_intervals"),
        Input(L.CLIENT_DROPDOWN_ID, "value"),
    )
    def update_suggestions(_n, client_id):
        if not client_id:
            if dash.ctx.triggered_id == L.POLL_INTERVAL_ID:
                return dash.no_update
            return [html.Li("Select a client to see suggestions.", className="empty")]
        stats = api_get(f"/clients/{client_id}/stats")
        if stats is None:
            return dash.no_update
        suggestions = stats.get("suggestions", [])
        if not suggestions:
            return [html.Li("No suggestions triggered.", className="empty")]
        return [
            html.Li(
                className="suggestion",
                children=[
                    html.Span("informational suggestion", className="badge"),
                    html.Span(s["text"]),
                ],
            )
            for s in suggestions
        ]


def _count_figure(stats: dict, range_mode: str):
    if range_mode == "day":
        points = stats.get("per_day", [])
        aggregated = {}
        for p in points:
            date_str = p.get("date", "")
            key = date_str[5:] if len(date_str) >= 10 else date_str
            aggregated[key] = aggregated.get(key, 0) + p.get("count", 1)
        title = "Coughs per day — last 7 days"
    else:
        points = stats.get("per_hour", [])
        aggregated = {}
        for p in points:
            # Hỗ trợ cả trường hợp key là 'ts' hoặc 'time'
            ts = p.get("ts", "") or p.get("time", "")
            if "T" in ts and len(ts.split("T")[1]) >= 2:
                hour_str = ts.split("T")[1][:2]
                key = f"{hour_str}:00"
            elif " " in ts and len(ts.split(" ")[1]) >= 2:
                hour_str = ts.split(" ")[1][:2]
                key = f"{hour_str}:00"
            elif len(ts) >= 13:
                key = f"{ts[11:13]}:00"
            else:
                key = "12:00"
            
            aggregated[key] = aggregated.get(key, 0) + p.get("count", 1)
        title = "Coughs per hour — last 24 h"

    sorted_keys = sorted(aggregated.keys())
    x = sorted_keys
    y = [aggregated[k] for k in sorted_keys]

    fig = px.bar(x=x, y=y)
    fig.update_traces(
        marker_color=_SERIES,
        marker_line_width=0,
        hovertemplate="%{x} — %{y} coughs<extra></extra>",
    )
    _apply_chrome(fig, title)
    fig.update_layout(
        bargap=0.35, yaxis_title=None, xaxis_title=None, xaxis_type="category"
    )
    return fig

def _type_figure(stats: dict):
    by_type = stats.get("by_type", {})
    names = [t for t in ("dry", "wet", "unknown") if by_type.get(t)]
    fig = px.pie(
        names=names,
        values=[by_type[t] for t in names],
        color=names,
        color_discrete_map=_TYPE_COLORS,
        hole=0.55,
    )
    # 2px surface gap between segments + direct labels, so identity is never
    # carried by color alone.
    fig.update_traces(
        marker=dict(line=dict(color=_SURFACE, width=2)),
        textinfo="label+percent",
        textfont=dict(size=12),
        hovertemplate="%{label}: %{value} coughs<extra></extra>",
    )
    _apply_chrome(fig, "Cough type breakdown")
    fig.update_layout(
        showlegend=True,
        legend=dict(orientation="h", y=-0.1, font=dict(size=12)),
    )
    return fig


def _placeholder_figure(message: str):
    fig = go.Figure()
    _apply_chrome(fig, "")
    fig.update_layout(
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[
            dict(
                text=message,
                showarrow=False,
                font=dict(size=13, color=_MUTED),
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
            )
        ],
    )
    return fig


def _apply_chrome(fig, title: str) -> None:
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, color=_INK)),
        height=300,
        margin=dict(l=44, r=16, t=44, b=36),
        paper_bgcolor=_SURFACE,
        plot_bgcolor=_SURFACE,
        font=dict(family=_FONT, size=12, color=_INK_2),
        showlegend=False,
        xaxis=dict(gridcolor=_GRID, linecolor=_BASELINE, zeroline=False),
        yaxis=dict(gridcolor=_GRID, linecolor=_BASELINE, zeroline=False),
    )


def _fmt_ts(iso: str | None) -> str:
    """Parse ISO safely and show the configured local timezone."""
    if not iso:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(iso)


def _fmt_number(value, suffix: str) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return "—"
