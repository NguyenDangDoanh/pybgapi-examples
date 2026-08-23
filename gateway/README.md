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

## Device connectivity status

Each connection that has completed GATT setup sends an independent best-effort
status heartbeat every 10 seconds. Heartbeats are never placed in the backend
retry FIFO, so cough, environment, connect, and disconnect messages retain
their ordering and queue capacity. A disconnect event marks the device offline
immediately. As a fallback, `/api/devices` marks an online device offline when
its `last_seen` proof of life is more than 30 seconds old; stale expiration does
not overwrite `last_seen`. Initial connection, heartbeat, and valid cough or
environment packets refresh `last_seen` from gateway receive time, never from a
historical cough `event_ts`.

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
raw flags and all decoded fields. Current firmware sends one notification when
a cough bout is completed: `event_ts` is the bout start, `duration_s` is its
estimated duration, and `event_counter` is the completed-bout counter. The
dashboard's Cough events table shows the firmware cough type; prolonged and
duration fields remain available in storage/API metadata for future use and
are not presented as a clinical severity assessment.

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

## Current data flow and timestamp responsibilities

The two timestamps have deliberately different responsibilities:

| Field | Meaning | Used by |
| --- | --- | --- |
| `event_ts` | When the patient actually coughed | Patient timeline, Cough events ordering, 24-hour/7-day charts, type/period stacks, daily totals, and baseline |
| `received_ts` | When the Pi received or replayed the notification | Transport audit, reconnect diagnostics, database ordering for the default audit API |
| `node_event_timestamp` | Raw uint32 sent by the firmware | Diagnosing whether the node sent a synchronized epoch or legacy zero |
| `timestamp_source` | `node_unix_seconds` or `gateway_received` | Explaining how the stored `event_ts` was selected |
| `event_counter` | Per-node uint16 completed-bout sequence | Duplicate/missing-event checks, wrap handling, and offline replay |

The processing path is:

1. The xG26 captures the bout start time and completed-bout metadata.
2. The Pi decodes the fixed 8-byte notification without changing its wire
   format.
3. `EventProcessor` stores both patient occurrence time and Pi receipt time.
   For legacy `event_ts == 0`, occurrence time safely falls back to receipt
   time. For extended `event_ts > 0`, the node time is preserved.
4. SQLite retains raw flags, decoded bout metadata, counter, both timestamps,
   and timestamp provenance.
5. Analytics query by `event_ts`; delayed FIFO replay therefore returns to the
   hour and day in which the cough really occurred.
6. Flask exposes the stored data and Dash renders the patient view. The BLE
   host, backend socket, API, and dashboard remain independent stages.

For example, a bout captured at 01:15 and delivered after an 08:00 reconnect is
shown at 01:15. The 08:00 receipt remains in SQLite for diagnostics but cannot
move the bout into the reconnect window.

## Dashboard behavior

The doctor dashboard contains:

- a Device list above the patient view, with assignment, connection status,
  warning state, latest environment values, and last-seen time;
- the patient selector and **Last cough event** (`MAX(event_ts)`) inside Cough
  monitoring;
- one total KPI and one full-width chart controlled by **24 HOURS / 7 DAYS**;
- the existing EWMA Statistical finding plus automatic treatment-week
  response; and
- an independent patient **Live feed** table with optional date filtering.

### Range anchoring

The two chart ranges have deliberately different definitions:

```text
24-hour range = [current wall-clock time - 24 hours, current wall-clock time]
7-day range   = seven completed local calendar days immediately before today
```

A late `received_ts` from offline replay never moves an event into a patient
chart: all chart filters use `event_ts`. A quiet period remains visible because
the 24-hour right edge follows current time, not the most recent cough.

The 24-hour chart uses clock-aligned local 30-minute buckets (`:00` and `:30`).
Each bar stacks Dry, Wet, and Unknown, shows its nonzero total above the stack,
and has a compact hover card containing only the interval and three type counts.
Empty buckets remain present. The 7-day chart
has one bar per completed date, with Day (`06:00-21:59`) and Night
(`22:00-05:59`) stacked in the same bar. Hovering one segment reports only that
period's bout total and Dry/Wet/Unknown breakdown. Today is excluded. If fewer
than seven completed observed dates are
available, the chart shows a warm-up message instead of fabricating a mature
seven-bar history.

Only the 7-day view overlays the current personal EWMA as one muted dashed
horizontal line with a direct **Personal baseline N.N/day** label. Neither chart has a Plotly
modebar, zoom, pan, or a line joining bar peaks.

### Live feed

The event table is independent of the chart range. Patient selection filters
the whole dashboard; **24 HOURS / 7 DAYS** affects only the KPI/chart and
baseline overlay; the table's Date control affects only the table. With
**All dates**, Time includes date and clock time. With one date selected, Time
shows the local clock only. The date dropdown contains only local dates that
actually have patient events, so future, pre-monitoring, and internal gap dates
cannot be selected. Rows are newest-first by `event_ts`, paginated at
25 rows, and contain only Time, Type, and Device. There is no Received time or
separate bout-duration column. `received_ts` remains stored and available to
transport/audit APIs.

The former `Suggestions` engine and API field have been removed. Its two
automatic messages duplicated the baseline presentation or compared adjacent
24-hour transport windows without adding patient context. The remaining
statistical area contains the EWMA finding and the separate treatment-response
summary described below.

The dashboard client/device selectors suppress legacy `client_test_alert`,
`device_test_alert`, and unassigned rows. This presentation filter does not
delete database records automatically.

## Personal baseline

The only baseline concept is **Personal baseline: Personal EWMA**, with
`alpha = 0.2`. Seven completed observed days are required for warm-up. It
continues updating after warm-up and is not a rolling seven-day window.
Missing/unavailable days are omitted instead of being invented as zero. The
first seven observed days always use `alpha = 0.2`. After warm-up, each
completed day is classified against the baseline and threshold that existed
before that day's update. A normal day keeps `alpha = 0.2`; an abnormal day
still participates but uses `alpha = 0.05`, preventing one spike from sharply
raising future thresholds while allowing slow adaptation to sustained change.
The treatment-response EWMA remains unchanged. The
finding compares rolling 24-hour count `C24` with EWMA `B`. Its threshold is:

```text
T = B + max(B * 0.40, 5)
```

The displayed change is `(C24 - B) / B * 100`. The 40% rule is an engineering
statistical threshold, not a clinical emergency threshold. There is one
whole-day baseline; Day and Night do not have separate baselines.
Visible device warning labels are **Calibrating**, **Normal**, **Warning**, and
**High Alert**. Their stable internal values remain `calibrating`, `normal`,
`needs_review`, and `high_priority`, so API consumers and row-color rules stay
compatible.
The current payload does not contain the number of individual cough sounds
inside a bout, so the gateway does not infer that quantity or run a second
bout-grouping state machine.

## Treatment response

Treatment response does **not** replace the EWMA finding. It starts
automatically on the first local calendar date with a valid patient event; the
dashboard has no treatment-date picker and collects no medication name, dose,
questionnaire, or treatment episode. The two calculations have different jobs:

- **EWMA finding**: checks whether rolling 24-hour count `C24` is above the
  patient's Personal EWMA threshold;
- **Treatment response**: compares completed seven-day blocks from the first
  data day.

```text
Week 1                    = formation period
Evaluation reference     = Personal EWMA snapshot at the start of that week
Week N average            = average across observed dates in the completed week
Change (%)                = (Week N average - EWMA reference) / EWMA reference * 100
```

A partial current week is shown as in progress and is never compared directly.
Missing monitoring dates are not converted into zero; an incomplete evaluation
week is labelled unavailable instead of being overinterpreted.
A negative percentage means fewer bouts and a positive percentage means more
bouts than the cumulative prior-week baseline. The result is descriptive and
does not label treatment effective or ineffective.

## Optional dashboard simulator

Simulated data is never created during normal gateway startup. The default
command backfills isolated historical scenarios and then keeps generating
stochastic live events:

```bash
python -m gateway.app.simulate_dashboard_data --replace
```

`--replace` removes both current `dashboard-sim-*` rows and the exact patient,
device, and message identifiers created by the retired `seed_demo_data.py`.
Real patient/device rows are not deleted.

Use `--history-only` to backfill and exit, or `--live-only` to append live data
without a backfill. `--seed` makes the generated history reproducible;
`--time-scale` accelerates only live waiting intervals and never creates future
timestamps. `--db /path/to/cough_monitor.db` selects another database.

The scenarios cover stable, worsening, treatment-improving, EWMA warmup, and
irregular/missing monitoring. Event times are stochastic and circadian, with
quiet periods, bursts, sparse nighttime events, per-patient cough-type mixes,
and rare prolonged bouts. All simulator patients, devices, and messages use the
reserved `dashboard-sim-` prefix. `--replace` removes only those records; real
gateway data is untouched.

While live simulation is running, it refreshes each simulated device's
connectivity status every 10 seconds without creating cough or environment
rows. This keeps simulated devices Online under the same 30-second freshness
rule as physical devices without changing their analytics. Per-profile rolling
24-hour caps keep accelerated live generation from pushing every scenario into
High Alert; once a cap is reached, only connectivity heartbeats continue until
older cough events leave the 24-hour window.

## BGM220 transport recovery

The BLE host supervises the pyBGAPI reader thread rather than relying only on
`BGLib.is_open()`. If the BGM220 is unplugged or the reader thread dies, the
current host stops connected heartbeats, reports every running node
disconnected, clears stale BLE state, and closes the old BGLib instance. The
supervisor then creates a new `BleCentral` every 2 seconds until the original
serial path is available again. Reconnection performs the complete boot, scan,
GATT discovery, notification setup, and optional time-sync sequence before a
new connected status is emitted.

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
and persistence, wall-clock rolling windows despite delayed replay,
occurrence-ordered paginated event queries and available-date navigation,
30-minute type stacks, completed seven-day Day/Night/type grouping, automatic
treatment weeks with EWMA reference snapshots, C24/EWMA warning levels,
simulator isolation/reproducibility, EWMA warm-up/threshold/missing-day behavior,
client isolation, two
nodes using the same event counter, duplicate replay across reconnect, uint16
wrap, environment persistence and validation, old-database migration,
concurrent socket clients, per-connection BLE notification routing, optional
Time discovery/write, isolated write failure, and fleet-wide UTC-date
resynchronization.
