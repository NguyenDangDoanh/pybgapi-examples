CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    client_id   TEXT,
    assigned_at DATETIME,
    status      TEXT,
    last_seen   DATETIME
);

CREATE TABLE IF NOT EXISTS cough_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL,
    client_id     TEXT,
    cough_type    TEXT,
    event_ts      DATETIME,
    received_ts   DATETIME NOT NULL,
    event_counter INTEGER,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);
