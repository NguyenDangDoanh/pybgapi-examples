"""Minimal doctor-facing BreathSense dashboard layout."""

from dash import dash_table, dcc, html

POLL_INTERVAL_ID = "poll-interval"
CLIENT_DROPDOWN_ID = "client-dropdown"
RANGE_TOGGLE_ID = "range-toggle"
FLEET_TABLE_ID = "fleet-table"
COUNT_CHART_ID = "count-chart"
LIVE_FEED_TABLE_ID = "live-feed-table"
TODAY_COUNT_ID = "today-count"
TOTAL_LABEL_ID = "total-label"
LAST_EVENT_ID = "last-event"
BASELINE_STATUS_ID = "baseline-status"
TREATMENT_RESPONSE_ID = "treatment-response"
COUGH_EVENT_DATE_ID = "cough-event-date"
COUGH_EVENT_EMPTY_ID = "cough-event-empty"

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


def _monitoring_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.H2("Cough monitoring"),
            html.Div(
                className="patient-card",
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
                            html.Span("Last cough event", className="field-label"),
                            html.Strong("—", id=LAST_EVENT_ID),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="monitoring-header",
                children=[
                    html.Div(
                        className="primary-kpi",
                        children=[
                            html.Span(
                                "Cough bouts — last 24 hours",
                                id=TOTAL_LABEL_ID,
                                className="kpi-label",
                            ),
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
                        config={
                            "displayModeBar": False,
                            "scrollZoom": False,
                            "doubleClick": False,
                        },
                        className="chart",
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
            html.Div(className="finding-divider"),
            html.Div(
                className="treatment-heading",
                children=html.Strong("Treatment response"),
            ),
            html.Div(
                id=TREATMENT_RESPONSE_ID,
                children=html.P(
                    "Select a patient to evaluate treatment-week trends.",
                    className="empty-state",
                ),
            ),
        ],
    )


def _live_feed_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.Div(
                className="events-heading",
                children=[
                    html.H2("Cough events"),
                    html.Div(
                        className="event-date-field",
                        children=[
                            html.Label("Date", className="field-label"),
                            dcc.DatePickerSingle(
                                id=COUGH_EVENT_DATE_ID,
                                display_format="YYYY-MM-DD",
                                clearable=True,
                                placeholder="All dates",
                            ),
                        ],
                    ),
                ],
            ),
            dash_table.DataTable(
                id=LIVE_FEED_TABLE_ID,
                columns=[
                    {"name": "Time", "id": "time"},
                    {"name": "Type", "id": "type"},
                    {"name": "Device", "id": "device"},
                ],
                page_action="custom",
                page_current=0,
                page_count=0,
                page_size=25,
                **_TABLE_KWARGS,
            ),
            html.P(
                "",
                id=COUGH_EVENT_EMPTY_ID,
                className="empty-state event-empty",
                style={"display": "none"},
            ),
        ],
    )
