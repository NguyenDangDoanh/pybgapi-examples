"""Flask REST API over the gateway data layer."""

from __future__ import annotations

from datetime import date

from flask import Flask, abort, jsonify, request

from .analytics import Analytics
from .dao import Dao
from .fleet import Fleet


def _limit_arg(default: int = 50) -> int:
    value = request.args.get("limit", default=default, type=int)
    return min(max(value or default, 1), 500)


def _optional_limit_arg() -> int | None:
    if "limit" not in request.args:
        return None
    return _limit_arg()


def create_app(dao: Dao, analytics: Analytics, fleet: Fleet) -> Flask:
    app = Flask(__name__)

    @app.get("/api/clients")
    def list_clients():
        return jsonify(dao.get_client_summaries())

    @app.get("/api/clients/<client_id>/events")
    def client_events(client_id: str):
        order = request.args.get("order", "received").strip().lower()
        query = {
            "client_id": client_id,
            "start_time": request.args.get("from"),
            "end_time": request.args.get("to"),
            "limit": _optional_limit_arg(),
        }
        if order == "event":
            events = dao.get_events_by_occurrence(
                **query,
                descending=True,
            )
        elif order == "received":
            events = dao.get_events(**query)
        else:
            abort(400, description="order must be 'event' or 'received'")
        return jsonify(events)

    @app.get("/api/clients/<client_id>/events/page")
    def client_event_page(client_id: str):
        """Return occurrence-ordered events for a paginated dashboard table."""
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        page_size = min(max(request.args.get("page_size", 25, type=int) or 25, 1), 100)
        start_time = request.args.get("from")
        end_time = request.args.get("to")
        items = dao.get_events_by_occurrence(
            client_id=client_id,
            start_time=start_time,
            end_time=end_time,
            limit=page_size,
            descending=True,
            offset=(page - 1) * page_size,
        )
        total = dao.count_events_by_occurrence(
            client_id=client_id,
            start_time=start_time,
            end_time=end_time,
        )
        return jsonify(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    @app.get("/api/clients/<client_id>/stats")
    def client_stats(client_id: str):
        return jsonify(analytics.get_client_stats(client_id))

    @app.get("/api/clients/<client_id>/event-dates")
    def client_event_dates(client_id: str):
        return jsonify(analytics.event_dates(client_id))

    @app.get("/api/clients/<client_id>/treatment")
    def get_client_treatment(client_id: str):
        return jsonify(dao.get_client_settings(client_id))

    @app.put("/api/clients/<client_id>/treatment")
    def set_client_treatment(client_id: str):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "treatment_start_date" not in data:
            abort(400, description="Body must contain treatment_start_date")
        treatment_start_date = data["treatment_start_date"]
        if treatment_start_date is not None:
            if not isinstance(treatment_start_date, str):
                abort(400, description="treatment_start_date must be YYYY-MM-DD or null")
            treatment_start_date = treatment_start_date.strip()
            try:
                treatment_start_date = date.fromisoformat(
                    treatment_start_date
                ).isoformat()
            except ValueError:
                abort(400, description="treatment_start_date must be YYYY-MM-DD or null")
        return jsonify(
            dao.set_treatment_start_date(client_id, treatment_start_date)
        )

    @app.get("/api/devices")
    def list_devices():
        devices = dao.get_devices()
        warning_cache: dict[str, dict] = {}
        for device in devices:
            client_id = device.get("client_id")
            if not client_id:
                device.update(
                    warning_level="unavailable",
                    warning_label="No patient data",
                )
                continue
            if client_id not in warning_cache:
                warning_cache[client_id] = analytics.ewma_baseline_status(client_id)
            warning = warning_cache[client_id]
            device.update(
                warning_level=warning.get("warning_level", "unavailable"),
                warning_label=warning.get("warning_label", "No recent data"),
                warning_baseline=warning.get("baseline"),
                warning_threshold=warning.get("threshold"),
                warning_c24=warning.get("c24"),
                warning_ratio=warning.get("ratio"),
                consecutive_abnormal_days=warning.get("consecutive_abnormal_days", 0),
            )
        return jsonify(devices)

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
