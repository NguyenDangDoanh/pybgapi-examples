# BreathSense gateway

## Runtime flow

```text
xG26 node(s)
  -> BLE GATT notifications
BGM220 NCP + Raspberry Pi BLE host
  -> JSON Lines over /tmp/cough_gw.sock
Gateway EventProcessor
  -> SQLite (cough_events + environment_readings + devices)
Flask API
  -> Dash dashboard on port 8050
```

The active BLE implementation is `gateway/ble_host_modular/`. The older
`gateway/ble_host/ble_central.py` is kept as a legacy single-file reference and
should not be run at the same time.

## Start order

From the repository root, activate the existing virtual environment and start
the backend/dashboard first:

```bash
source .venv/bin/activate
python -m gateway.app.main
```

Then start the BLE host in another SSH terminal:

```bash
source .venv/bin/activate
PORT="/dev/serial/by-id/usb-Silicon_Labs_J-Link_OB_000440210672-if00"
python gateway/ble_host_modular/main.py "$PORT" \
  --xapi api/sl_bt.xapi \
  --name-prefix MyDevice \
  --service-uuid b5e00001-7a4b-4c6d-9e10-112233445566 \
  --cough-uuid b5e00002-7a4b-4c6d-9e10-112233445566 \
  --environment-uuid b5e00003-7a4b-4c6d-9e10-112233445566 \
  --backend-socket /tmp/cough_gw.sock \
  --max-connections 2 \
  -l INFO
```

`--max-connections` defaults to 2 for the current two-node requirement. Lower
or raise it explicitly if the flashed BGM220 NCP configuration supports a
different number of simultaneous BLE links.

## Timestamp policy

- `received_ts`: UTC time at which the Pi receives the notification.
- `event_ts`: node Unix time when it is plausible and synchronized; otherwise
  it falls back to `received_ts`.
- `node_event_timestamp`: raw uint32 sent by xG26.
- `timestamp_source`: records whether node time or gateway receive time was
  used. Current firmware sends zero, so the dashboard shows Pi receive time.
- Dashboard display defaults to `Asia/Ho_Chi_Minh`; override with
  `GATEWAY_TIMEZONE`.

## Optional environment variables

```bash
export GATEWAY_DB_PATH="$HOME/pybgapi-examples/cough_monitor.db"
export GATEWAY_SOCKET_PATH="/tmp/cough_gw.sock"
export GATEWAY_HOST="0.0.0.0"
export GATEWAY_PORT="8050"
export GATEWAY_TIMEZONE="Asia/Ho_Chi_Minh"
export GATEWAY_LOG_LEVEL="INFO"
```

## Verification

```bash
python -m compileall -q gateway tests
python -m unittest discover -s tests -v
```

The tests cover two nodes using the same event counter, duplicate retries,
counter reset and reconnect, environment persistence and validation,
old-database migration, concurrent socket clients, and per-connection BLE
notification routing.
