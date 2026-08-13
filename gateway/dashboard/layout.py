"""Minimal doctor-facing BreathSense dashboard layout."""

from dash import dash_table, dcc, html

POLL_INTERVAL_ID = "poll-interval"
CLIENT_DROPDOWN_ID = "client-dropdown"
RANGE_TOGGLE_ID = "range-toggle"
FLEET_TABLE_ID = "fleet-table"
COUNT_CHART_ID = "count-chart"
TYPE_PIE_ID = "type-pie"
LIVE_FEED_TABLE_ID = "live-feed-table"
TODAY_COUNT_ID = "today-count"
LAST_RECEIVED_ID = "last-received"
DAY_COUNT_ID = "day-count"
NIGHT_COUNT_ID = "night-count"
BASELINE_STATUS_ID = "baseline-status"

POLL_INTERVAL_MS = 4000
_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

_TABLE_KWARGS = dict(
    style_as_list_view=True,
    cell_selectable=False,
    style_table={"overflowX": "auto"},
    style_cell={
        "fontFamily": _FONT,
        "fontSize": "13px",
        "color": "#0b0b0b",
        "backgroundColor": "transparent",
        "padding": "8px 12px",
        "textAlign": "left",
        "border": "none",
        "borderBottom": "1px solid #e1e0d9",
    },
    style_header={
        "fontWeight": "600",
        "fontSize": "12px",
        "color": "#898781",
        "textTransform": "uppercase",
        "letterSpacing": "0.04em",
        "borderBottom": "1px solid #c3c2b7",
    },
)


def make_layout() -> html.Div:
    return html.Div(
        className="app",
        children=[
            html.Header(
                className="masthead",
                children=[
                    html.H1("BreathSense"),
                    html.Span("Objective cough-bout monitoring", className="subtitle"),
                ],
            ),
            dcc.Interval(id=POLL_INTERVAL_ID, interval=POLL_INTERVAL_MS),
            _patient_section(),
            _fleet_section(),
            _monitoring_section(),
            _baseline_section(),
            _live_feed_section(),
        ],
    )


def _fleet_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.H2("Device list"),
            dash_table.DataTable(
                id=FLEET_TABLE_ID,
                columns=[
                    {"name": "Device", "id": "device"},
                    {"name": "Status", "id": "status"},
                    {"name": "Patient", "id": "patient"},
                    {"name": "Temperature", "id": "temperature"},
                    {"name": "Humidity", "id": "humidity"},
                    {"name": "Last seen", "id": "last_seen"},
                ],
                page_size=8,
                **_TABLE_KWARGS,
            ),
        ],
    )


def _patient_section() -> html.Div:
    return html.Div(
        className="card patient-card",
        children=[
            html.Div(
                children=[
                    html.Label("Patient", className="field-label"),
                    dcc.Dropdown(
                        id=CLIENT_DROPDOWN_ID,
                        placeholder="Select a patient...",
                        className="client-dropdown",
                    ),
                ]
            ),
            html.Div(
                className="last-received-block",
                children=[
                    html.Span("Last data received", className="field-label"),
                    html.Strong("—", id=LAST_RECEIVED_ID),
                ],
            ),
        ],
    )


def _monitoring_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.H2("Cough monitoring"),
            html.Div(
                className="monitoring-header",
                children=[
                    html.Div(
                        className="primary-kpi",
                        children=[
                            html.Span("Cough bouts today", className="kpi-label"),
                            html.Strong("—", id=TODAY_COUNT_ID),
                        ],
                    ),
                    dcc.RadioItems(
                        id=RANGE_TOGGLE_ID,
                        options=[
                            {"label": "24 HOURS", "value": "24h"},
                            {"label": "7 DAYS", "value": "7d"},
                        ],
                        value="24h",
                        inline=True,
                        className="range-toggle",
                    ),
                ],
            ),
            html.Div(
                className="charts-row",
                children=[
                    dcc.Graph(
                        id=COUNT_CHART_ID,
                        config={"displayModeBar": False},
                        className="chart",
                    ),
                    dcc.Graph(
                        id=TYPE_PIE_ID,
                        config={"displayModeBar": False},
                        className="chart chart-narrow",
                    ),
                ],
            ),
            html.Div(
                className="day-night-row",
                children=[
                    html.Div(
                        className="period-stat",
                        children=[html.Span("Day"), html.Strong("—", id=DAY_COUNT_ID)],
                    ),
                    html.Div(
                        className="period-stat",
                        children=[
                            html.Span("Night"),
                            html.Strong("—", id=NIGHT_COUNT_ID),
                        ],
                    ),
                ],
            ),
        ],
    )


def _baseline_section() -> html.Div:
    return html.Div(
        className="card baseline-card",
        children=[
            html.H2("Statistical findings"),
            html.Div(
                id=BASELINE_STATUS_ID,
                children=html.P(
                    "Select a patient to evaluate the recent personal baseline.",
                    className="empty-state",
                ),
            ),
        ],
    )


def _live_feed_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.H2("Live Feed"),
            dash_table.DataTable(
                id=LIVE_FEED_TABLE_ID,
                columns=[
                    {"name": "Event time", "id": "event_time"},
                    {"name": "Received time", "id": "received_time"},
                    {"name": "Event", "id": "event"},
                ],
                page_size=10,
                **_TABLE_KWARGS,
            ),
        ],
    )
