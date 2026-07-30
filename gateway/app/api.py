"""Flask REST API — thin routes over dao, analytics, rules, fleet.

Endpoint table in project_info.md.  Each route parses request params, calls
exactly one module, and returns jsonify(...).  No business logic in routes.
The Dash dashboard mounts on this same Flask instance.

See design/gateway_app.md.
"""

from __future__ import annotations

from flask import Flask, abort, jsonify, request

from .analytics import Analytics
from .dao import Dao
from .fleet import Fleet
from .rules import Rules


def create_app(dao: Dao, analytics: Analytics, rules: Rules, fleet: Fleet) -> Flask:
    """Build and return the Flask application with all REST routes registered."""
    app = Flask(__name__)

    @app.get("/api/clients")
    def list_clients():
        """List clients with summary stats."""
        clients = dao.get_client_summaries()
        return jsonify(clients)

    @app.get("/api/clients/<client_id>/events")
    def client_events(client_id: str):
        """Cough events for one client. Accepts ?from= and ?to= ISO timestamps."""
        from_time = request.args.get("from")
        to_time = request.args.get("to")
        events = dao.get_events(
            client_id=client_id,
            start_time=from_time,
            end_time=to_time,
        )
        return jsonify(events)

    @app.get("/api/clients/<client_id>/stats")
    def client_stats(client_id: str):
        """Aggregates: counts by type, per-hour/day trends, suggestions."""
        stats = analytics.get_client_stats(client_id)
        stats["suggestions"] = [
            {"rule": suggestion.rule, "text": suggestion.text}
            for suggestion in rules.evaluate(client_id)
        ]
        return jsonify(stats)

    @app.get("/api/devices")
    def list_devices():
        """Fleet list with status, current client, and last_seen."""
        devices = dao.get_devices()
        return jsonify(devices)

    @app.post("/api/devices/<device_id>/assign")
    def assign_device(device_id: str):
        """Assign or reassign a client_id. Body: {"client_id": "C-0042"} or null."""
        data = request.get_json(silent=True) or {}
        client_id = data.get("client_id")

        fleet.assign(device_id, client_id)
        return jsonify({
            "status": "success",
            "device_id": device_id,
            "client_id": client_id,
        })

    @app.get("/api/events/recent")
    def recent_events():
        """Latest events across the fleet — powers the dashboard live feed."""
        limit = request.args.get("limit", default=50, type=int)
        events = dao.get_recent_events(limit=limit)
        return jsonify(events)

    @app.post("/api/feedback")
    def feedback():
        """[Could] Provider feedback on detection accuracy."""
        abort(501)

    return app
