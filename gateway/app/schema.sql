CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    name         TEXT,
    address_type INTEGER,
    client_id    TEXT,
    assigned_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'offline',
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS cough_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           TEXT,
    session_id           TEXT,
    device_id            TEXT NOT NULL,
    client_id            TEXT,
    cough_type           TEXT,
    event_ts             TEXT NOT NULL,
    received_ts          TEXT NOT NULL,
    event_counter        INTEGER,
    node_event_timestamp INTEGER,
    timestamp_source     TEXT,
    flags                INTEGER,
    timestamp_valid      INTEGER,
    stage2_valid         INTEGER,
    prolonged            INTEGER,
    duration_s           INTEGER,
    payload_hex          TEXT,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS environment_readings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT,
    session_id          TEXT,
    device_id           TEXT NOT NULL,
    client_id           TEXT,
    event_ts            TEXT NOT NULL,
    received_ts         TEXT NOT NULL,
    temperature_c       REAL,
    humidity_percent    REAL,
    temperature_x100    INTEGER,
    humidity_x100       INTEGER,
    payload_hex         TEXT,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS client_settings (
    client_id            TEXT PRIMARY KEY,
    treatment_start_date TEXT,
    updated_at           TEXT NOT NULL
);

-- Durable store-and-forward queue. Records are never removed automatically;
-- sent=1 means the remote server acknowledged the same event_id.
CREATE TABLE IF NOT EXISTS telemetry_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT UNIQUE NOT NULL,
    event_ts        TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    sent            INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT,
    sent_at         TEXT,
    created_at      TEXT NOT NULL
);

-- Used by a remote BreathSense gateway/server to make uploads idempotent.
CREATE TABLE IF NOT EXISTS telemetry_receipts (
    event_id    TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cough_client_event
    ON cough_events(client_id, event_ts DESC);

CREATE INDEX IF NOT EXISTS ix_telemetry_outbox_pending
    ON telemetry_outbox(sent, id);
