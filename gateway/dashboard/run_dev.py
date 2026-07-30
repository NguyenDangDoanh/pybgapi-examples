"""Dev-only entry point for WS-D (task D1): serves the dashboard against an
in-memory mock of the gateway REST API, so the dashboard can be developed and
demoed with zero hardware before gateway/app (WS-C) exists.

The mock implements the endpoint table from project_info.md and the stats shape
from design/gateway_app.md. One extension: the stats response carries a
"suggestions" list (the endpoint table has no suggestions route) — agree on
this with WS-C before D3.

Run from the repo root:

    python -m gateway.dashboard.run_dev
"""

import os
import random
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

from gateway.dashboard.dash_app import create_dash

HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8050"))
RNG = random.Random(42)

flask_app = Flask(__name__)

_DEVICES = {
    "xg26-01": {"device_id": "xg26-01", "client_id": "C-0042", "status": "online"},
    "xg26-02": {"device_id": "xg26-02", "client_id": "C-0017", "status": "online"},
}
_events: list[dict] = []
_lock = threading.Lock()
_next_live_event_at = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_event(device: dict, ts: datetime) -> dict:
    return {
        "id": 0,  # renumbered after seeding
        "device_id": device["device_id"],
        "client_id": device["client_id"],
        "cough_type": RNG.choices(["dry", "wet", "unknown"], weights=[60, 35, 5])[0],
        "event_ts": _iso(ts),
        "received_ts": _iso(ts + timedelta(seconds=RNG.uniform(1, 8))),
    }


def _seed_history() -> None:
    """7 days of plausible history; xg26-01 coughs ~2.5x more in the last 24 h
    so the rate_doubled suggestion fires in the demo."""
    now = datetime.now(timezone.utc)
    for device in _DEVICES.values():
        base_rate = 1.5 if device["device_id"] == "xg26-01" else 0.8
        for hours_ago in range(7 * 24, 0, -1):
            slot = now - timedelta(hours=hours_ago)
            mu = base_rate * (1.0 if 8 <= slot.hour < 22 else 0.3)
            if device["device_id"] == "xg26-01" and hours_ago <= 24:
                mu *= 2.5
            for _ in range(max(0, round(RNG.gauss(mu, 0.9)))):
                ts = slot + timedelta(minutes=RNG.uniform(0, 59))
                if ts < now:
                    _events.append(_make_event(device, ts))
    _events.sort(key=lambda e: e["received_ts"])
    for i, event in enumerate(_events, start=1):
        event["id"] = i


def _maybe_generate_live_event() -> None:
    """Called on every API hit: emits a new event every 15-45 s so the live
    feed visibly updates within the 4 s polling interval."""
    global _next_live_event_at
    with _lock:
        now = datetime.now(timezone.utc)
        if now >= _next_live_event_at:
            device = RNG.choice(list(_DEVICES.values()))
            event = _make_event(device, now)
            event["id"] = _events[-1]["id"] + 1 if _events else 1
            _events.append(event)
            _next_live_event_at = now + timedelta(seconds=RNG.uniform(15, 45))


def _client_events(client_id: str) -> list[dict]:
    return [e for e in _events if e["client_id"] == client_id]


@flask_app.get("/api/clients")
def clients():
    _maybe_generate_live_event()
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(hours=24))
    return jsonify(
        [
            {
                "client_id": client_id,
                "total_24h": sum(
                    1
                    for e in _client_events(client_id)
                    if e["received_ts"] >= cutoff
                ),
            }
            for client_id in sorted(
                {d["client_id"] for d in _DEVICES.values() if d["client_id"]}
            )
        ]
    )


@flask_app.get("/api/clients/<client_id>/events")
def client_events(client_id):
    # from/to query params accepted but ignored by the mock.
    _maybe_generate_live_event()
    return jsonify(list(reversed(_client_events(client_id)[-50:])))


@flask_app.get("/api/clients/<client_id>/stats")
def client_stats(client_id):
    _maybe_generate_live_event()
    events = _client_events(client_id)
    now = datetime.now(timezone.utc)

    by_type = {"dry": 0, "wet": 0, "unknown": 0}
    for event in events:
        by_type[event["cough_type"]] += 1

    per_hour = []
    for hours_ago in range(23, -1, -1):
        bucket = (now - timedelta(hours=hours_ago)).replace(
            minute=0, second=0, microsecond=0
        )
        lo, hi = _iso(bucket), _iso(bucket + timedelta(hours=1))
        per_hour.append(
            {
                "ts": lo,
                "count": sum(1 for e in events if lo <= e["received_ts"] < hi),
            }
        )

    per_day = []
    for days_ago in range(6, -1, -1):
        bucket = (now - timedelta(days=days_ago)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        lo, hi = _iso(bucket), _iso(bucket + timedelta(days=1))
        per_day.append(
            {
                "date": lo[:10],
                "count": sum(1 for e in events if lo <= e["received_ts"] < hi),
            }
        )

    # Mirror of rules.rule_rate_doubled from design/gateway_app.md.
    suggestions = []
    today = sum(p["count"] for p in per_hour)
    yesterday_lo = _iso(now - timedelta(hours=48))
    yesterday_hi = _iso(now - timedelta(hours=24))
    yesterday = sum(
        1 for e in events if yesterday_lo <= e["received_ts"] < yesterday_hi
    )
    if yesterday > 0 and today > 2 * yesterday:
        suggestions.append(
            {
                "rule": "rate_doubled",
                "text": (
                    f"Cough rate more than doubled vs. yesterday "
                    f"({today} vs. {yesterday} in 24 h). Consider reviewing "
                    f"this client's recent events."
                ),
            }
        )

    return jsonify(
        {
            "total": len(events),
            "by_type": by_type,
            "per_hour": per_hour,
            "per_day": per_day,
            "suggestions": suggestions,
        }
    )


@flask_app.get("/api/devices")
def devices():
    _maybe_generate_live_event()
    now = datetime.now(timezone.utc)
    return jsonify(
        [
            {
                **device,
                "last_seen": _iso(now - timedelta(seconds=RNG.uniform(2, 28))),
            }
            for device in _DEVICES.values()
        ]
    )


@flask_app.post("/api/devices/<device_id>/assign")
def assign(device_id):
    device = _DEVICES.get(device_id)
    if device is None:
        return jsonify({"error": "unknown device"}), 404
    device["client_id"] = (request.get_json(silent=True) or {}).get("client_id")
    return jsonify(device)


@flask_app.get("/api/events/recent")
def events_recent():
    _maybe_generate_live_event()
    return jsonify(list(reversed(_events[-50:])))


def main() -> None:
    _seed_history()
    create_dash(flask_app)
    flask_app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
