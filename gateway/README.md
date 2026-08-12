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
  --time-uuid b5e00004-7a4b-4c6d-9e10-112233445566 \
  --backend-socket /tmp/cough_gw.sock \
  --max-connections 2 \
  -l INFO
```

`--max-connections` defaults to 2 for the current two-node requirement. Lower
or raise it explicitly if the flashed BGM220 NCP configuration supports a
different number of simultaneous BLE links.

## Timestamp policy

- `received_ts`: UTC time at which the Pi receives the notification.
- `event_ts`: when the raw node value is greater than zero, the Pi uses that
  Unix timestamp. When the raw value is zero, it falls back to `received_ts`.
- `node_event_timestamp`: raw uint32 sent by xG26.
- `timestamp_source`: records whether node time or gateway receive time was
  used (`node_unix_seconds` or `gateway_received`).
- Dashboard display defaults to `Asia/Ho_Chi_Minh`; override with
  `GATEWAY_TIMEZONE`.

`gateway/app/event_processor.py` preserves `node_event_timestamp`,
`event_counter`, `timestamp_source`, and `received_ts`; node time changes do
not replace or remove those audit fields.

## Time synchronization contract

### GATT UUIDs

| Item | UUID | Contract |
| --- | --- | --- |
| BreathSense service | `b5e00001-7a4b-4c6d-9e10-112233445566` | Primary service |
| Cough Event | `b5e00002-7a4b-4c6d-9e10-112233445566` | Notify, fixed 8-byte payload |
| Environment | `b5e00003-7a4b-4c6d-9e10-112233445566` | Notify |
| Time | `b5e00004-7a4b-4c6d-9e10-112233445566` | Optional Read + Write, uint32 Unix epoch, little-endian |

The Cough Event payload must remain `struct <BBIH>`:

```text
flags:uint8 | cough_type:uint8 | event_ts:uint32 | event_counter:uint16
```

The cough-bout firmware assigns the flag bits as follows:

| Bits | Field | Meaning |
| --- | --- | --- |
| 0 | `timestamp_valid` | Firmware had a synchronized epoch when the bout event was created |
| 1 | `stage2_valid` | Stage 2 had enough confidence to classify dry/wet |
| 2 | `prolonged` | Bout duration reached the configurable prolonged threshold |
| 3–7 | `duration_s` | Estimated bout duration, saturated at 31 seconds |

`UNKNOWN` is still a valid cough event, including when `prolonged=true`; it
means Stage 2 did not confidently choose dry or wet. The gateway persists the
raw flags and all decoded fields. The live and client event tables display the
estimated duration and label prolonged bouts as `requires observation`. This
is a monitoring indication, not a diagnosis.

The authoritative backward-compatible timestamp rule remains the raw value:
`event_ts > 0` uses node Unix time and `event_ts == 0` uses `received_ts`.
`timestamp_valid` is retained as firmware metadata and does not replace that
rule. Firmware must not put device uptime in `event_ts`.

There is no second cough payload format and no uint16 `delta_seconds since
disconnect` representation. Legacy firmware keeps sending the same payload
with `event_ts = 0`. Extended firmware sends a synchronized `event_ts > 0`.

### Connect, reconnect, and daily sync

Discovery and procedures are tracked independently by connection handle. For
each node the Pi discovers the service and all characteristics, enables Cough
Event notifications, enables Environment notifications, and then writes the
current epoch if the optional Time characteristic is present and writable. A
missing Time characteristic identifies a compatible legacy node; notifications
continue normally.

The gateway uses a write-with-response procedure and waits for its GATT
completion asynchronously. Reading Time back is useful for diagnostics but is
not required for normal FIFO flushing; the firmware begins flushing only after
it accepts the write.

On reconnect, extended firmware must wait until notifications are enabled and
the new epoch has been accepted before flushing its offline FIFO. The Time
write is asynchronous from the Pi event loop. A command or procedure failure
is recorded for that node only and does not stop notifications or another
node's discovery/time state.

The Pi uses one UTC-date boundary check for the fleet. When the date changes at
UTC midnight, it starts a new Time write for every eligible connected node.
This is deliberately not a per-connection 24-hour timer; nodes connected at
different times still resynchronize at the same UTC boundary. A node that is
still connecting at the boundary receives the normal connect-time sync.

### Firmware monotonic clock

A BLE disconnect must not stop or reset the firmware's time source. On a valid
Time write, firmware records:

```text
sync_epoch = received_unix_epoch
sync_tick = monotonic_now()
event_epoch = sync_epoch + elapsed_seconds(sync_tick, monotonic_now())
```

The monotonic counter must continue while BLE is disconnected. Capture
`event_epoch` when the cough actually occurs, not when it is transmitted.
Updating `sync_epoch`/`sync_tick` after reconnect changes timestamps for future
events only.

### Offline buffering and counters

Store each offline cough event with all four wire fields: `flags`,
`cough_type`, `event_ts`, and `event_counter`. Using the same 8-byte `<BBIH>`
record avoids a second serialization contract. On reconnect, replay buffered
records in FIFO order without replacing their captured `event_ts` with the
reconnect time.

Keep `event_counter` as a continuously incrementing uint16 value. The gateway
uses it per node and host session to reject duplicate replay, report missing
events, recognize the `65535 -> 0` wrap, and distinguish a backward reset from
a wrap. Reconnect does not clear the gateway's counter state.

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

The tests cover legacy and extended timestamps, fixed-size bout-flag decoding
and persistence, two nodes using the same event counter, duplicate replay
across reconnect, uint16 wrap, environment persistence and validation,
old-database migration, concurrent socket clients, per-connection BLE
notification routing, optional Time discovery/write, isolated write failure,
and fleet-wide UTC-date resynchronization.
