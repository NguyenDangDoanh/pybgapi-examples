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
raw flags and all decoded fields. Current firmware sends one notification when
a cough bout is completed: `event_ts` is the bout start, `duration_s` is its
estimated duration, and `event_counter` is the completed-bout counter. The
Live Feed labels rows as `Cough bout` or `Prolonged bout`; prolonged remains
firmware monitoring metadata, not a clinical severity assessment.

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
| `event_ts` | When the patient actually coughed | Patient timeline, Live Feed ordering, 24-hour/7-day charts, Wet/Dry/Unknown, Day/Night, daily totals, and baseline |
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
  latest environment values, and last-seen time;
- the patient selector and **Last cough event** (`MAX(event_ts)`) inside Cough
  monitoring;
- the observed cough-bout count for the local calendar date containing the
  patient's latest event;
- a 24-hour hourly trend or 7-day daily trend;
- Wet/Dry/Unknown distribution computed from exactly the selected range;
- Day (06:00-17:59) and Night (18:00-05:59) totals from that same range;
- one recent personal bout baseline and a patient-specific Live Feed.

### Range anchoring

With no explicit analysis time supplied, the selected patient's latest
`event_ts` is the single analysis anchor:

```text
24-hour range = [latest event_ts - 24 hours, latest event_ts]
7-day range   = local date of latest event plus the six preceding local dates
```

This anchor is shared by the trend, type distribution, Day/Night totals, daily
count, and baseline. A late `received_ts` from offline replay never extends or
shifts the patient window. When a patient or range changes, or a genuinely
newer cough arrives, the graph returns naturally to the latest anchored view.

The 24-hour chart groups events into single-color local 30-minute bars. Every
bar is the total number of completed cough bouts whose `event_ts` is inside
that interval; Wet/Dry/Unknown never creates stacked bars. The right edge is
the selected patient's latest cough time, integer Y ticks are used, and hover
text is intentionally short (`08:00–08:30`, `4 bouts`). Horizontal dragging
can inspect earlier history without exposing zoom controls or connecting bar
peaks.

The 7-day chart uses one bar per local date. It draws exactly one horizontal
reference line at the current Personal EWMA computed from completed prior
usable days. This is not a historical EWMA trajectory. The active day's bar
may be visible, but that incomplete day does not update the line.

### Live Feed

Live Feed is always visible and is sorted newest-first by `event_ts`. It shows:

- **Event time**: the patient's cough time;
- **Event**: Wet/Dry/Unknown, `Cough bout` or `Prolonged bout`, and firmware
  duration when present.

The optional local Event date picker defaults to **All dates**. Its UTC query
bounds are derived from the selected local date and filter `event_ts`, so an
offline replay remains on the date when the patient coughed. Clearing the
picker restores all events and the table keeps its existing pagination.

There is no separate BOUT column and no Received time column. `received_ts`
has not been deleted: it remains in SQLite and is still returned by the API for
transport audit. `/api/clients/<client_id>/events` defaults to receipt order;
the dashboard requests `?order=event` for occurrence order.

The former `Suggestions` engine and cumulative treatment UI are not used by
the dashboard.

The dashboard client/device selectors suppress legacy `client_test_alert`,
`device_test_alert`, and unassigned rows. This presentation filter does not
delete database records automatically.

## Dashboard analytics contract

### Personal EWMA

Each patient has one long-running Personal EWMA with `alpha = 0.2`. Monitoring
starts automatically on the first valid local event date. That first date is
shown as the start marker but conservatively treated as partial, so it is not
one of the seven completed usable warm-up days. Missing/unavailable dates are
omitted rather than invented as zero monitoring days.

After warm-up, the Personal EWMA continues updating every completed usable
day. The active day is excluded. A day is compared with the pre-update
baseline; only after completion can it update the value used by the following
day. The existing abnormal-day threshold remains available to API consumers:

```text
EWMA baseline + max(EWMA baseline * 0.40, 5)
```

The current payload does not contain the number of individual cough sounds
inside a bout, so the gateway does not infer that quantity or run a second
bout-grouping state machine. Each received cough event is already one bout.

### Day and Night

Visible Day/Night cards contain counts only. Day is 06:00–17:59 local time;
Night is 18:00–05:59 and belongs to the local date on which it starts. Day and
Night maintain independent internal EWMA streams with the same `alpha = 0.2`
and seven-completed-period warm-up. Hover may show Increasing, Decreasing, or
Stable, using a centralized ±10% tolerance. It never exposes the internal
baseline value. The currently active incomplete period has no trend result.

### Sequential treatment weeks

Treatment weeks start automatically after the partial first date and are
sequential blocks of seven completed usable days, not calendar weeks. There is
no manual treatment date and no cumulative arithmetic treatment baseline.

Week 1 forms the Personal EWMA baseline and only reports progress such as
`5 / 7 completed days`. For Week 2 and later, the analytics layer captures a
fixed start-of-week snapshot of the Personal EWMA. The main Personal EWMA still
updates after each completed usable day; the snapshot does not move.

A temporary `weekly_level` is calculated by applying EWMA only to the seven
counts inside that week, resetting at every week boundary. It is a weekly
summary, not another baseline. No result is published until all seven usable
days are complete:

```text
Change (%) = (weekly_level - week_reference_snapshot)
             / week_reference_snapshot * 100
```

A zero reference produces `N/A`. Statistical Findings shows only the active
week's progress and the latest completed evaluable week; it does not render a
week-history chart.

The legacy treatment-setting API and `client_settings.treatment_start_date`
remain accepted for database/API compatibility, but the dashboard no longer
reads or writes them:

```text
GET /api/clients/<client_id>/treatment
PUT /api/clients/<client_id>/treatment
Body: {"treatment_start_date": "2026-08-09"}
Clear: {"treatment_start_date": null}
```

## Optional dashboard demo data

Demo data is never created during normal gateway startup. To exercise every
dashboard state against the configured SQLite database, run:

```bash
python -m gateway.app.seed_demo_data --replace
```

Use `--db /path/to/cough_monitor.db` when `GATEWAY_DB_PATH` is not set and the
database is elsewhere. The command creates only records whose message IDs use
the reserved `demo-dashboard-` prefix plus these states:

- `demo_patient_warmup`: Personal EWMA formation at 4/7 usable days;
- `demo_patient_week2_incomplete`: Week 2 calculating at 3/7 usable days;
- `demo_patient_week3`: active Week 3 with a completed Week 2 result.

Together they include clustered 30-minute bars, all cough types, Day/Night
history, prolonged labels, environment readings, and a replay whose
`event_ts` and `received_ts` fall on different local dates.

Running without `--replace` leaves existing demo rows untouched. Running with
`--replace` deletes and rebuilds only rows created by this generator; real
patient events and environment readings are not modified.

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
and persistence, latest-event range anchoring despite delayed replay,
occurrence-ordered Live Feed queries, range-specific cough types, 30-minute
totals, Day/Night grouping and independent trend warm-up, Personal EWMA
warm-up/continuous update/missing-day behavior, fixed weekly snapshots,
temporary weekly EWMA, zero-reference handling, client isolation, two
nodes using the same event counter, duplicate replay across reconnect, uint16
wrap, environment persistence and validation, old-database migration,
concurrent socket clients, per-connection BLE notification routing, optional
Time discovery/write, isolated write failure, and fleet-wide UTC-date
resynchronization.
