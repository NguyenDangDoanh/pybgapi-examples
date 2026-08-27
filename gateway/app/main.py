"""Gateway entry point: concurrent socket ingest plus Flask/Dash."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from gateway.dashboard.dash_app import create_dash

from .analytics import Analytics
from .api import create_app
from .dao import Dao
from .event_processor import EventProcessor
from .fleet import Fleet
from .socket_server import SocketServer
from .telemetry_queue import TelemetryQueue
from .upload_worker import worker_from_environment

HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8050"))
REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = os.environ.get("GATEWAY_DB_PATH", str(REPO_ROOT / "cough_monitor.db"))
SOCKET_PATH = os.environ.get("GATEWAY_SOCKET_PATH", "/tmp/cough_gw.sock")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dao = Dao(DB_PATH)
    dao.init_db(SCHEMA_PATH)
    dao.mark_all_offline()
    telemetry_queue = TelemetryQueue(DB_PATH)
    telemetry_queue.init_db()

    fleet = Fleet(dao)
    analytics = Analytics(dao)
    processor = EventProcessor(dao, fleet, telemetry_queue)
    upload_worker = worker_from_environment(telemetry_queue)
    upload_worker.start()
    socket_server = SocketServer(sock_path=SOCKET_PATH)
    socket_thread = threading.Thread(
        target=socket_server.serve_forever,
        args=(processor.process,),
        daemon=True,
        name="gateway-ingest-server",
    )
    socket_thread.start()
    if not socket_server.ready.wait(timeout=3.0):
        upload_worker.stop()
        dao.close()
        raise SystemExit("Gateway socket server did not become ready")
    if socket_server.startup_error is not None:
        upload_worker.stop()
        dao.close()
        raise SystemExit(f"Gateway socket startup failed: {socket_server.startup_error}")

    flask_app = create_app(
        dao,
        analytics,
        fleet,
        processor=processor,
        telemetry_queue=telemetry_queue,
    )
    create_dash(flask_app)
    try:
        flask_app.run(host=HOST, port=PORT, debug=False, threaded=True)
    finally:
        upload_worker.stop()
        dao.close()


if __name__ == "__main__":
    main()
