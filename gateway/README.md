# BreathSense gateway

## Runtime flow

```text
xG26 node(s)
  -> BLE GATT notifications
BGM220 NCP + Raspberry Pi BLE host
  -> JSON Lines over /tmp/cough_gw.sock
Gateway EventProcessor
  -> SQLite domain tables + durable telemetry_outbox (commit first)
Flask API
  -> Dash dashboard on port 8050
Upload worker (independent thread)
  -> oldest pending event -> remote POST -> ACK event_id -> mark sent
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
python gateway/ble_host_modular/main.py \
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

### Known physical-device assignments

Fixed real-device assignments are defined once in
`gateway/app/device_assignments.py`:

| BLE address | Patient |
| --- | --- |
| `54:dc:e9:32:21:ac` | `client_01` |
| `64:02:8f:64:12:88` | `client_08` |

`Fleet` reapplies these mappings whenever the gateway starts, stores them in
the `devices` table, and repairs existing cough/environment rows belonging to
the same physical device. Status, heartbeat, environment, cough, reconnect,
dashboard restart, and Pi reboot therefore cannot turn either known device
into Unassigned or assign it to another patient. The normal assignment API
continues to work for devices not present in the fixed mapping.

The Patient dropdown includes assigned device patients even before they have a
cough event. A new `client_08` can therefore show the normal
insufficient-history state instead of disappearing from the dashboard.

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

The Device list exposes only its seven intended columns. Row colors are based
on the visible Warning value, so the API's internal `warning_level` field does
not require a hidden DataTable column and Dash does not render a **Toggle
Columns** control. Pagination remains eight rows per page.

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

Simulated data is never created during normal gateway startup. The command
creates one static historical dataset and exits; it does not append live
events or refresh simulated device status afterward:

```bash
python -m gateway.app.simulate_dashboard_data --replace
```

`--replace` is safe for routine testing and is idempotent. Before generating
the new static dataset it removes simulator-owned cough, environment, device,
patient-setting, outbox, and receipt records. Ownership is recognized by the
reserved `dashboard-sim-*` message/session/device prefix, the retired
`demo-dashboard-*` prefix, and the exact legacy demo patient/device IDs from
`seed_demo_data.py`. It does not delete unrelated real patient/device rows, so
running the same seed repeatedly replaces rather than accumulates history.

`--seed` makes the generated history reproducible. `--db
/path/to/cough_monitor.db` selects another database.

The gateway, simulator, and maintenance command all resolve the database path
the same way: explicit `--db`, then `GATEWAY_DB_PATH`, then
`<repo-root>/cough_monitor.db`. A relative configured path is anchored at the
repository root, not the process working directory. Each command logs the
absolute resolved path.

### Explicit full dashboard reset

For a test database that must be returned to a completely blank state, stop
writers, back up the file, and run the deliberately destructive command:

```bash
cp cough_monitor.db "cough_monitor.db.backup-$(date +%Y%m%d-%H%M%S)"
python -m gateway.app.reset_dashboard_data --yes
python -m gateway.app.simulate_dashboard_data --replace
```

Use `--db /absolute/path/to/cough_monitor.db` on both commands when selecting a
non-default database. Without `--yes`, reset exits without changing anything.
The reset keeps the schema but atomically clears `telemetry_receipts`,
`telemetry_outbox`, `cough_events`, `environment_readings`, `client_settings`,
and `devices`, resets their applicable AUTOINCREMENT counters, and then
vacuum-compacts the file. If any delete fails, the entire transaction rolls
back. Use `--no-vacuum` only when page reclamation should be deferred.

Unlike `--replace`, full reset intentionally deletes real and simulated data.
It refuses to create or guess a missing database path.

The scenarios cover stable, worsening, treatment-improving, EWMA warmup, and
irregular/missing monitoring. Event times are stochastic and circadian, with
quiet periods, bursts, sparse nighttime events, per-patient cough-type mixes,
and rare prolonged bouts. Simulator device and message identifiers retain the
reserved `dashboard-sim-` prefix so `--replace` can remove only owned records;
real gateway data is untouched. Patient names shown by the dashboard are:

| Scenario | Patient |
| --- | --- |
| Irregular / missing | `client_02` |
| Warning / needs review | `client_03` |
| Stable | `client_04` |
| Treatment improving | `client_05` |
| EWMA warmup | `client_06` |
| Worsening | `client_07` |

Because the dataset is static, simulated devices naturally become Offline
under the same freshness rule used for physical devices. Their historical
events, warning scenarios, charts, and environment samples remain available.

## BGM220 transport recovery

The BLE host supervises the pyBGAPI reader thread rather than relying only on
`BGLib.is_open()`. If the BGM220 is unplugged or the reader thread dies, the
current host stops connected heartbeats, reports every running node
disconnected, clears stale BLE state, and closes the old BGLib instance. The
supervisor scans current serial ports every 2 seconds. It ranks Silicon
Labs/SEGGER/J-Link metadata but does not trust VID alone: every candidate must
pass a disposable `bt.system.hello()` handshake. The probe is closed, and the
selected path is used to construct a completely new `SerialConnector`,
`BGLib`, and `BleCentral`. Therefore `/dev/ttyACM0` may become `ttyACM1` or
`ttyACM2` without restarting Python. A periodic active hello also detects a
dead command path even when a device node or reader object still exists.

An explicit positional path is still accepted for compatibility and is tried
first, but auto-discovery remains the fallback. Use
`--bgm220-serial-number SERIAL` to select one board when several candidates
are attached.

## Durable store-and-forward

Every accepted cough/environment message is committed to
`telemetry_outbox` in the same persistent SQLite file before it can enter the
remote upload path. The upload worker uses its own short-lived SQLite
connections, reads pending rows oldest-first, and marks `sent=1` only after a
2xx response contains an ACK for the identical `event_id`. Failed records are
never deleted and have no retry limit. Backoff grows from 5 seconds to 5
minutes, then retries continue indefinitely. The captured `event_ts` and the
full original JSON payload are retained across Wi-Fi loss, server downtime,
process restart, and Pi reboot.

`POST /api/telemetry` is the matching idempotent receiver for a remote
BreathSense server. It stores a `telemetry_receipts` row keyed by `event_id`;
retries return a duplicate ACK rather than creating a second event.

The queue does not impose a time or record-count retention limit. Disk usage,
database size, and pending count are logged periodically. Usage at 80% logs a
warning and 90% logs a critical error; unsent rows are not silently removed.
Queue status is available at `GET /api/telemetry/queue`.

## Optional environment variables

```bash
export GATEWAY_DB_PATH="$HOME/pybgapi-examples/cough_monitor.db"
export GATEWAY_SOCKET_PATH="/tmp/cough_gw.sock"
export GATEWAY_HOST="0.0.0.0"
export GATEWAY_PORT="8050"
export GATEWAY_TIMEZONE="Asia/Ho_Chi_Minh"
export GATEWAY_LOG_LEVEL="INFO"
export GATEWAY_UPLOAD_URL="https://server.example/api/telemetry"
export GATEWAY_UPLOAD_BATCH_SIZE="100"
export GATEWAY_UPLOAD_TIMEOUT_SECONDS="10"
export GATEWAY_UPLOAD_BACKOFF_INITIAL_SECONDS="5"
export GATEWAY_UPLOAD_BACKOFF_MAX_SECONDS="300"
export GATEWAY_DISK_CHECK_SECONDS="60"
export GATEWAY_DISK_WARN_PERCENT="80"
export GATEWAY_DISK_CRITICAL_PERCENT="90"
```

Leave `GATEWAY_UPLOAD_URL` unset to keep collecting durable pending telemetry
without making Internet requests. Setting it later resumes the oldest pending
row; it does not change the original event timestamp.

## systemd user services

Templates are in `gateway/systemd/`. They use absolute paths derived from
`%h`, restart crashed processes, and start the BGM220 host in auto-discovery
mode. Install once on the Pi:

```bash
mkdir -p ~/.config/systemd/user ~/.config/breathsense
cp gateway/systemd/breathsense-*.service ~/.config/systemd/user/
cp gateway/systemd/gateway.env.example ~/.config/breathsense/gateway.env
systemctl --user daemon-reload
systemctl --user enable --now \
  breathsense-dashboard.service breathsense-bgm220.service
sudo loginctl enable-linger "$USER"
```

Edit `~/.config/breathsense/gateway.env` before enabling remote upload. The
simulator is intentionally not an auto-start production service.

### Database migration

No manual data migration is required. Startup adds `telemetry_outbox`,
`telemetry_receipts`, and their indexes using `CREATE ... IF NOT EXISTS`.
Existing cough, environment, device, treatment, timestamp, and counter rows are
left unchanged.

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
simulator isolation/reproducibility/idempotent replacement, explicit full
dashboard reset and rollback, shared database-path resolution, real-row safety,
EWMA warm-up/threshold/missing-day behavior,
client isolation, two
nodes using the same event counter, duplicate replay across reconnect, uint16
wrap, environment persistence and validation, old-database migration,
concurrent socket clients, per-connection BLE notification routing, optional
Time discovery/write, isolated write failure, and fleet-wide UTC-date
resynchronization. Resilience tests additionally cover serial candidate
ranking, multi-device BGAPI handshake selection, fresh-port resolution after
transport loss, active hello failure, durable queue persistence, retry without
deletion, timestamp preservation, and ACK-based completion.

### Raspberry Pi acceptance procedure

Run the dashboard and BLE services through systemd, then follow the cases
below while watching both logs and the durable queue:

```bash
journalctl --user -fu breathsense-dashboard.service &
journalctl --user -fu breathsense-bgm220.service &
watch -n 2 'curl -s http://127.0.0.1:8050/api/telemetry/queue'
```

1. Start with BGM220 on `ttyACM0`; verify the scan, successful hello, and node
   notifications without passing a serial path.
2. Unplug and reconnect it on the same port; verify disconnect and reconnect
   without a Python restart.
3. Force/reproduce a change to `ttyACM1` or `ttyACM2`; verify the newly scanned
   path is used.
4. Attach another serial device; verify rejected handshake(s) followed by the
   BGM220 handshake and selection.
5. Block the upload destination for 30 minutes while EFR32 continues sending;
   verify `pending` increases, then returns to zero after access is restored.
6. Repeat the offline test for one day; there is no offline-duration timeout or
   retry limit.
7. Reboot the Pi with pending rows; verify they remain after service startup and
   upload in FIFO order after network recovery.
8. Keep Wi-Fi available but stop the server; verify upload failures retain rows,
   then resume after the server restarts.
9. POST the same envelope/event ID twice to `/api/telemetry`; verify both calls
   ACK but only one domain record and one `telemetry_receipts` row exist.
10. With pending rows and upload blocked, unplug/reconnect BGM220, then restore
    the server. Verify USB recovery and pending upload proceed independently and
    neither service crashes.

For cases 5-10, compare event IDs and original event timestamps at the source,
in `telemetry_outbox`, and at the receiver. A pass requires equal record sets,
no duplicate event IDs, preserved occurrence timestamps, and no unsent row
being deleted. Journald stores both service logs and applies the host's normal
log retention policy; inspect recent history with
`journalctl --user -u SERVICE --since today`.
