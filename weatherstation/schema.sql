PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS weather_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id TEXT NOT NULL,
    source_name TEXT,
    msg_id INTEGER NOT NULL,
    source_ts_utc TEXT NOT NULL,
    received_at_utc TEXT NOT NULL,
    temp_c REAL NOT NULL,
    humidity_pct REAL NOT NULL,
    pressure_hpa REAL NOT NULL,
    wind_ms REAL NOT NULL,
    wind_dir_deg INTEGER NOT NULL,
    rain_mm REAL NOT NULL,
    wind_lull_ms REAL,
    wind_gust_ms REAL,
    wind_sample_interval_s INTEGER,
    illuminance_lux REAL,
    uv_index REAL,
    solar_radiation_wm2 REAL,
    precipitation_type INTEGER,
    lightning_avg_distance_km REAL,
    lightning_strike_count INTEGER,
    battery_voltage_v REAL,
    report_interval_min INTEGER,
    local_day_rain_mm REAL,
    nearcast_rain_mm REAL,
    local_day_nearcast_rain_mm REAL,
    precipitation_analysis_type INTEGER,
    weather_timestamp INTEGER,
    raw_payload TEXT NOT NULL,
    payload_hash TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_node_id, source_ts_utc)
);

CREATE INDEX IF NOT EXISTS idx_weather_received_at
ON weather_readings(received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_received
ON weather_readings(source_node_id, received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_msg
ON weather_readings(source_node_id, msg_id);

CREATE TABLE IF NOT EXISTS aws_delivery_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_utc TEXT,
    last_attempt_at_utc TEXT,
    delivered_at_utc TEXT,
    last_error TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(reading_id) REFERENCES weather_readings(id) ON DELETE CASCADE,
    UNIQUE(reading_id)
);

CREATE INDEX IF NOT EXISTS idx_delivery_status_next_attempt
ON aws_delivery_queue(status, next_attempt_at_utc, id);

CREATE TABLE IF NOT EXISTS device_health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id TEXT NOT NULL,
    source_name TEXT,
    msg_id INTEGER,
    source_ts_utc TEXT,
    received_at_utc TEXT NOT NULL,
    status TEXT NOT NULL,
    uptime_sec INTEGER,
    ip_address TEXT,
    error_reason TEXT,
    raw_payload TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_health_source_received
ON device_health_events(source_node_id, received_at_utc);

CREATE TABLE IF NOT EXISTS weather_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id TEXT NOT NULL,
    source_name TEXT,
    msg_id INTEGER,
    event_type TEXT NOT NULL,
    source_ts_utc TEXT,
    received_at_utc TEXT NOT NULL,
    lightning_distance_km REAL,
    lightning_energy INTEGER,
    raw_payload TEXT NOT NULL,
    payload_hash TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_weather_events_source_received
ON weather_events(source_node_id, received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_events_type_received
ON weather_events(event_type, received_at_utc);

CREATE TABLE IF NOT EXISTS device_telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id TEXT NOT NULL,
    source_name TEXT,
    msg_id INTEGER,
    telemetry_type TEXT NOT NULL,
    source_ts_utc TEXT,
    received_at_utc TEXT NOT NULL,
    uptime_sec INTEGER,
    firmware_revision TEXT,
    rssi INTEGER,
    hub_rssi INTEGER,
    sensor_status INTEGER,
    debug INTEGER,
    voltage REAL,
    reset_flags TEXT,
    seq INTEGER,
    fs_json TEXT,
    radio_stats_json TEXT,
    mqtt_stats_json TEXT,
    raw_payload TEXT NOT NULL,
    payload_hash TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_telemetry_source_received
ON device_telemetry_events(source_node_id, received_at_utc);

CREATE INDEX IF NOT EXISTS idx_telemetry_type_received
ON device_telemetry_events(telemetry_type, received_at_utc);

CREATE TABLE IF NOT EXISTS device_status_current (
    source_node_id TEXT PRIMARY KEY,
    source_name TEXT,
    last_weather_at_utc TEXT,
    last_health_at_utc TEXT,
    last_device_status_at_utc TEXT,
    last_hub_status_at_utc TEXT,
    last_status TEXT,
    derived_state TEXT,
    last_telemetry_type TEXT,
    last_msg_id INTEGER,
    last_source_ts_utc TEXT,
    last_uptime_sec INTEGER,
    last_ip_address TEXT,
    last_firmware_revision TEXT,
    last_rssi INTEGER,
    last_hub_rssi INTEGER,
    last_sensor_status INTEGER,
    last_debug INTEGER,
    last_device_voltage_v REAL,
    last_reset_flags TEXT,
    last_hub_seq INTEGER,
    last_fs_json TEXT,
    last_radio_stats_json TEXT,
    last_mqtt_stats_json TEXT,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at_utc TEXT NOT NULL,
    source_node_id TEXT,
    source_name TEXT,
    packet_type TEXT NOT NULL,
    reason TEXT,
    msg_id INTEGER,
    raw_payload TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingest_received
ON ingest_events(received_at_utc);
