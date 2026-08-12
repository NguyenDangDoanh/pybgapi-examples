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
