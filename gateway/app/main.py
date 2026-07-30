"""Gateway application entry point.

Two threads in one process:
    1. Socket ingest (daemon) — reads JSON lines from ble_host
    2. Flask + Dash — serves REST API and the healthcare provider dashboard

They share only the Dao, whose SQLite operations are protected by a lock.

See design/gateway_app.md.
"""

from __future__ import annotations

import os
import threading

from gateway.dashboard.dash_app import create_dash

from .analytics import Analytics
from .api import create_app
from .dao import Dao
from .event_processor import EventProcessor
from .fleet import Fleet
from .rules import Rules
from .socket_server import SocketServer

HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8050"))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def main() -> None:
    """Initialise DB, start background threads, and serve Flask + Dash."""
    dao = Dao()
    dao.init_db(SCHEMA_PATH)

    fleet = Fleet(dao)
    analytics = Analytics(dao)
    rules = Rules(analytics)
    processor = EventProcessor(dao, fleet)

    socket_server = SocketServer()
    socket_thread = threading.Thread(
        target=socket_server.serve_forever,
        args=(processor.process,),
        daemon=True,
    )
    socket_thread.start()

    flask_app = create_app(dao, analytics, rules, fleet)
    create_dash(flask_app)
    flask_app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
