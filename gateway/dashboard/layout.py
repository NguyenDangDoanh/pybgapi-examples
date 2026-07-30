"""Page structure for the cough-monitor dashboard.

Every Dash component id lives in the constants below and nowhere else --
callbacks.py imports them, so layout and callbacks can never drift apart
(mismatched ids are the #1 Dash beginner bug, see design/dashboard_and_tools.md).
"""

from dash import dash_table, dcc, html

POLL_INTERVAL_ID = "poll-interval"
CLIENT_DROPDOWN_ID = "client-dropdown"
RANGE_TOGGLE_ID = "range-toggle"
FLEET_TABLE_ID = "fleet-table"
LIVE_FEED_TABLE_ID = "live-feed-table"
COUNT_CHART_ID = "count-chart"
TYPE_PIE_ID = "type-pie"
CLIENT_EVENTS_TABLE_ID = "client-events-table"
SUGGESTIONS_LIST_ID = "suggestions-list"

POLL_INTERVAL_MS = 4000

_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Shared DataTable look: list view, hairline rules, recessive header.
_TABLE_KWARGS = dict(
    style_as_list_view=True,
    cell_selectable=False,
    style_table={"overflowX": "auto"},
    style_cell={
        "fontFamily": _FONT,
        "fontSize": "13px",
        "color": "#0b0b0b",
        "backgroundColor": "transparent",
        "padding": "6px 12px",
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
                    html.H1("Cough Monitor"),
                    html.Span(
                        "Fleet dashboard — healthcare provider view",
                        className="subtitle",
                    ),
                ],
            ),
            dcc.Interval(id=POLL_INTERVAL_ID, interval=POLL_INTERVAL_MS),
            _fleet_section(),
            _client_section(),
            html.Div(
                className="row",
                children=[_live_feed_section(), _suggestions_section()],
            ),
        ],
    )


def _fleet_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.H2("Fleet overview"),
            dash_table.DataTable(
                id=FLEET_TABLE_ID,
                columns=[
                    {"name": "Device", "id": "device"},
                    {"name": "Status", "id": "status"},
                    {"name": "Client", "id": "client"},
                    {"name": "Temperature", "id": "temperature"},
                    {"name": "Humidity", "id": "humidity"},
                    {"name": "Last seen", "id": "last_seen"},
                ],
                **_TABLE_KWARGS,
            ),
        ],
    )


def _client_section() -> html.Div:
    return html.Div(
        className="card",
        children=[
            html.H2("Client detail"),
            html.Div(
                className="controls",
                children=[
                    dcc.Dropdown(
                        id=CLIENT_DROPDOWN_ID,
                        placeholder="Select a client…",
                        className="client-dropdown",
                    ),
                    dcc.RadioItems(
                        id=RANGE_TOGGLE_ID,
                        options=[
                            {"label": "Hourly · 24 h", "value": "hour"},
                            {"label": "Daily · 7 d", "value": "day"},
                        ],
                        value="hour",
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
            html.H3("Recent events for this client"),
            dash_table.DataTable(
                id=CLIENT_EVENTS_TABLE_ID,
                columns=[
                    {"name": "Time", "id": "time"},
                    {"name": "Device", "id": "device"},
                    {"name": "Type", "id": "type"},
                ],
                page_size=8,
                **_TABLE_KWARGS,
            ),
        ],
    )


def _live_feed_section() -> html.Div:
    return html.Div(
        className="card grow",
        children=[
            html.H2("Live feed"),
            dash_table.DataTable(
                id=LIVE_FEED_TABLE_ID,
                columns=[
                    {"name": "Received", "id": "time"},
                    {"name": "Device", "id": "device"},
                    {"name": "Client", "id": "client"},
                    {"name": "Type", "id": "type"},
                ],
                page_size=10,
                **_TABLE_KWARGS,
            ),
        ],
    )


def _suggestions_section() -> html.Div:
    return html.Div(
        className="card grow",
        children=[
            html.H2("Suggestions"),
            html.Ul(id=SUGGESTIONS_LIST_ID, className="suggestions"),
            html.P(
                "Informational suggestions derived from cough data — "
                "not clinical predictions or medical advice.",
                className="disclaimer",
            ),
        ],
    )
