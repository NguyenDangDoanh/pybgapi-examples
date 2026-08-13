"""Flask REST API over the gateway data layer."""

from __future__ import annotations

from flask import Flask, abort, jsonify, request

from .analytics import Analytics
from .dao import Dao
from .fleet import Fleet
from .rules import Rules


def _limit_arg(default: int = 50) -> int:
    value = request.args.get("limit", default=default, type=int)
    return min(max(value or default, 1), 500)


def _optional_limit_arg() -> int | None:
    if "limit" not in request.args:
        return None
    return _limit_arg()


def create_app(dao: Dao, analytics: Analytics, rules: Rules, fleet: Fleet) -> Flask:
    app = Flask(__name__)

    @app.get("/api/clients")
    def list_clients():
        return jsonify(dao.get_client_summaries())

    @app.get("/api/clients/<client_id>/events")
    def client_events(client_id: str):
        return jsonify(
            dao.get_events(
                client_id=client_id,
                start_time=request.args.get("from"),
                end_time=request.args.get("to"),
                limit=_optional_limit_arg(),
            )
        )

    @app.get("/api/clients/<client_id>/stats")
    def client_stats(client_id: str):
        stats = analytics.get_client_stats(client_id)
        stats["suggestions"] = [
            {
                "rule": suggestion.rule,
                "text": suggestion.text,
                "category": suggestion.category,
            }
            for suggestion in rules.evaluate(client_id)
        ]
        return jsonify(stats)

    @app.get("/api/devices")
    def list_devices():
        return jsonify(dao.get_devices())

    @app.post("/api/devices/<device_id>/assign")
    def assign_device(device_id: str):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "client_id" not in data:
            abort(400, description="Body must contain client_id")
        client_id = data["client_id"]
        if client_id is not None and not isinstance(client_id, str):
            abort(400, description="client_id must be a string or null")
        if isinstance(client_id, str):
            client_id = client_id.strip() or None
        fleet.assign(device_id, client_id)
        return jsonify(
            {"status": "success", "device_id": device_id, "client_id": client_id}
        )

    @app.get("/api/events/recent")
    def recent_events():
        return jsonify(dao.get_recent_events(limit=_limit_arg()))

    @app.get("/api/environment/recent")
    def recent_environment():
        return jsonify(dao.get_recent_environment(limit=_limit_arg()))

    @app.post("/api/feedback")
    def feedback():
        abort(501)

    return app
