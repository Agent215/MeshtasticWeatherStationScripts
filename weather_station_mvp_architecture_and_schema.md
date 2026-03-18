# Weather Station MVP: SQLite Schema, Home Server Code, and AWS Schema

Prepared from the currently provided MVP code and design docs.

## Files reviewed

### Design / requirements docs
- `weather_station_ai_handoff.docx`
- `home_pi_server_design_spec.docx`
- `weather_station_design_revised_v2 (2).docx`

### Home / edge code
- `listen_meshtastic.py`
- `parser.py`
- `db.py`
- `queue_worker.py`
- `schema.sql`
- `main.py` (Pico W UDP -> UART bridge; included because it defines the payloads the home server receives)
- `mock_tempest_udp_sender.py` (test generator for Tempest-style UDP)

### AWS code / infra
- `weather-station-stack.yaml`
- `packaged-template.yaml`
- `app.py` (AWS ingest Lambda)
- `appRead.py` (AWS read Lambda)

## Important note about one missing file

The home-side scripts import a `storage.py` module, but that file was **not** present in the uploaded files I could inspect directly. Because of that, the storage-layer behavior in this document is reconstructed from:

- `schema.sql`
- `db.py`
- the functions that call storage (`listen_meshtastic.py` and `queue_worker.py`)
- the design documents

That reconstruction is strong enough to explain the architecture and the database behavior, but where I describe the exact behavior of `insert_weather`, `insert_health`, `record_ingest_event`, `fetch_pending_deliveries`, `mark_delivery_success`, and `mark_delivery_failure`, I am inferring from the surrounding code and schema rather than quoting the missing implementation.

---

# 1. Executive summary

This MVP is a two-stage pipeline:

1. **Garden side**: a Pico W listens for Tempest UDP broadcasts, extracts weather values, and emits a compact JSON payload over UART to a Meshtastic radio.
2. **Home side**: a Raspberry Pi listens to Meshtastic text messages over USB serial, parses and validates them, stores accepted packets in **SQLite**, and later forwards pending weather readings to AWS.
3. **Cloud side**: API Gateway sends requests to Lambda, and Lambda writes to a single **DynamoDB** table that stores observations, the latest reading, and dedupe markers.

The key reliability pattern is:

- **SQLite is the local source of truth on the home server**.
- **DynamoDB is the cloud copy / serving layer**.
- The queue worker gives the system **at-least-once delivery** to AWS.
- AWS is built to tolerate duplicates by using a dedupe item keyed by station and message id.

---

# 2. End-to-end data flow

## 2.1 Physical and logical path

```text
Tempest sensor
  -> Tempest hub
  -> UDP broadcast on local LAN (port 50222)
  -> Pico W (`main.py`)
  -> UART JSON to garden Meshtastic node
  -> LoRa / Meshtastic mesh
  -> home Meshtastic node
  -> USB serial to Raspberry Pi
  -> `listen_meshtastic.py`
  -> `parser.py`
  -> SQLite (`schema.sql` via storage layer)
  -> `queue_worker.py`
  -> HTTPS POST /observations
  -> API Gateway
  -> ingest Lambda (`app.py`)
  -> DynamoDB (`WeatherObservations`)

View path:
Browser / site / trusted client
  -> GET /observations/latest or GET /observations
  -> API Gateway
  -> read Lambda (`appRead.py`)
  -> DynamoDB
```

## 2.2 Reliability model

The MVP uses a classic **store-and-forward** pattern:

- Home ingest does not try to post directly to AWS before saving locally.
- Weather messages are first committed to SQLite.
- A separate queue worker polls for pending deliveries.
- Failed cloud posts stay in the queue and are retried.
- Cloud-side writes are idempotent from the station/message-id perspective.

That means temporary AWS outages should not cause data loss as long as the Raspberry Pi and SQLite database remain healthy.

---

# 3. The home-side codebase: what each script does

## 3.1 `listen_meshtastic.py`: the ingest daemon

This is the Raspberry Pi process that listens to the home Meshtastic node over USB serial.

### Main responsibilities

- Opens a Meshtastic serial connection using `meshtastic.serial_interface.SerialInterface`.
- Subscribes to pubsub topics:
  - `meshtastic.receive`
  - `meshtastic.connection.established`
  - `meshtastic.connection.lost`
- Extracts packet metadata such as:
  - `fromId`
  - node long/short name
  - `toId`
  - `portnum`
  - RF metadata like RSSI / SNR / hop count
- Decodes the inbound text payload.
- Hands text payloads to `parser.py`.
- Writes successful results to SQLite through the missing `storage.py` layer.
- Reconnects automatically if the Meshtastic USB serial connection drops.

### Packet handling model

The script is intentionally conservative:

- It logs every received packet.
- It only tries to parse application payloads when `portnum == "TEXT_MESSAGE_APP"`.
- Telemetry packets are noticed and logged, but not inserted into the weather database.

### High-level flow inside `process_text_packet`

```text
Received packet
  -> decode payload text
  -> parse_text_payload(...)
  -> if weather:
       insert_weather(parsed)
       if duplicate: record_ingest_event(parsed)
     elif health:
       insert_health(parsed)
     else:
       record_ingest_event(parsed)
```

### Operational behavior

- Handles `SIGINT` and `SIGTERM` for clean shutdown.
- Loops forever while `RUNNING` is true.
- On connection error, waits 5 seconds and reconnects.
- Produces JSON log lines suitable for `journalctl` or log collection.

## 3.2 `parser.py`: packet classification, normalization, and validation

This is the boundary where raw Meshtastic text becomes structured application data.

### Output model

The parser returns a `ParsedEvent` dataclass with:

- `packet_type`
- `reason`
- `source_node_id`
- `source_name`
- `msg_id`
- `received_at_utc`
- `source_ts_utc`
- `raw_payload`
- `normalized`

### Supported packet classes

#### 1. `weather`
Triggered when the JSON object contains the required keys:

- `i` = message id
- `t` = temperature C
- `h` = humidity %
- `p` = pressure hPa
- `w` = wind m/s
- `d` = wind direction degrees
- `r` = rain mm

Optional:

- `ts` = source timestamp

The parser normalizes this to:

- `msg_id`
- `temp_c`
- `humidity_pct`
- `pressure_hpa`
- `wind_ms`
- `wind_dir_deg`
- `rain_mm`
- `source_ts_utc`

Validation ranges implemented in code:

- temp: `-60 .. 70`
- humidity: `0 .. 100`
- pressure: `800 .. 1200`
- wind: `0 .. 100`
- wind direction: `0 .. 360`
- rain: `0 .. 500`

#### 2. `health`
Triggered when the JSON object contains `sys`.

Normalized fields:

- `status`
- `msg_id`
- `uptime_sec`
- `ip_address`
- `error_reason`
- `source_ts_utc`

This means any payload shaped like:

```json
{"sys":"dbg","i":123,"up":900,"ip":"192.168.1.50"}
```

will be treated as a health/status event.

#### 3. `invalid`
Used when:

- JSON parsing fails
- JSON is not an object
- health parsing fails

Examples of reasons:

- `malformed_json`
- `json_not_object`
- `bad_health_payload:<exception>`

#### 4. `rejected`
Used when the weather payload has the right general shape but fails numeric conversion or range validation.

#### 5. `unknown`
Used when the payload is valid JSON but does not match the known weather or health contracts.

### Important limitation in the current parser

The current home parser only stores the **compact weather core**:

- temp
- humidity
- pressure
- average wind
- wind direction
- rain
- source timestamp

It does **not** currently normalize the richer extra fields sent by the latest Pico bridge, such as:

- gust
- lull
- UV
- solar radiation
- illuminance
- lightning fields
- battery voltage
- precipitation analysis fields

Those values exist upstream and the AWS Lambda can validate them, but the current parser/schema path does not yet persist them locally.

## 3.3 `db.py`: SQLite connection helper

This file is very small but important. It centralizes database connection behavior.

### What it does

- Builds the DB path as:

```text
~/weatherstation-home/weatherstation/weatherstation.db
```

- Opens a SQLite connection.
- Sets `row_factory = sqlite3.Row` so rows can be addressed by column name.
- Enables foreign keys.
- Enables WAL journal mode.

### Why this matters

- **`foreign_keys=ON`** ensures queue rows cannot reference nonexistent weather readings.
- **WAL mode** is a good fit here because it improves concurrent read/write behavior and reduces locking pain for a service-style app.

### Note on config drift

The design spec suggests an environment-configurable `SQLITE_PATH`, but the current uploaded `db.py` hardcodes the database location under the home directory. That is a meaningful difference between the design docs and the actual MVP code.

## 3.4 `queue_worker.py`: durable delivery worker

This is the second major home-side process. It reads pending weather rows from SQLite and POSTs them to AWS.

### Main responsibilities

- Loads API settings from environment / `.env`:
  - `API_URL`
  - `API_KEY`
- Polls SQLite for pending deliveries.
- Builds the outbound API request body.
- Sends `POST /observations` with `x-weatherstation-key`.
- Retries transient HTTP/network failures.
- Marks queue rows as success or failure in SQLite.
- Emits structured logs.
- Optionally notifies systemd via native notify socket support.

### Worker loop behavior

```text
startup
  -> load config
  -> every 5 seconds:
       fetch up to 10 pending rows
       for each row:
         build API payload
         POST to AWS with retry
         if success: mark_delivery_success(queue_id)
         else:       mark_delivery_failure(queue_id, error)
```

### Retry behavior

Retryable HTTP statuses are:

- `429`
- `500`
- `502`
- `503`
- `504`

Also retryable:

- `URLError`
- timeout

Non-retryable errors include other HTTP errors such as most validation failures or auth failures.

Backoff schedule per attempt:

- attempt 1 failure -> 2 seconds
- attempt 2 failure -> 4 seconds
- attempt 3 failure -> 8 seconds
- attempt 4 failure -> 16 seconds
- attempt 5 failure -> capped at 30 seconds

Maximum tries per processing cycle: **5**.

### Outbound payload shape

`queue_worker.py` converts SQLite rows into:

```json
{
  "payload": {
    "source_node_id": "!5c32d4d9",
    "source_name": null,
    "msg_id": 99,
    "source_ts_utc": "1773794743",
    "received_at_utc": "2026-03-18T00:45:46.982324Z",
    "weather": {
      "air_temp_c": 21.8,
      "relative_humidity_pct": 56.0,
      "station_pressure_hpa": 1011.9,
      "wind_avg_ms": 2.2,
      "wind_dir_deg": 240,
      "rain_interval_mm": 0.2
    }
  }
}
```

### Future-ready mapping

The worker already knows how to forward many richer fields if they exist in the DB row, including:

- `wind_lull_ms`
- `wind_gust_ms`
- `wind_sample_interval_s`
- `illuminance_lux`
- `uv_index`
- `solar_radiation_wm2`
- `precipitation_type`
- `lightning_avg_distance_km`
- `lightning_strike_count`
- `battery_voltage_v`
- `report_interval_min`
- `local_day_rain_mm`
- `nearcast_rain_mm`
- `local_day_nearcast_rain_mm`
- `precipitation_analysis_type`
- `timestamp`

However, the current `schema.sql` does **not** define columns for those richer fields in `weather_readings`, so in the current MVP most of those values will only be forwarded if the storage layer is synthesizing them from somewhere else. Most likely they are simply absent today and get dropped by `drop_none()`.

### Systemd integration

The worker implements a small native `SystemdNotifier` class that supports:

- `READY=1`
- `STATUS=...`
- `STOPPING=1`
- `WATCHDOG=1`

So if the final unit file uses `Type=notify` and systemd watchdog settings, the worker is already prepared for that.

### Note on config drift

The design docs describe variables like `AWS_API_URL` and `AWS_API_KEY`, but the actual uploaded worker looks for:

- `API_URL`
- `API_KEY`

That is another real difference between the design docs and the current MVP implementation.

## 3.5 `main.py`: Pico W producer context

This is not part of the home server, but it matters because it defines what the home server receives.

### What it does

- Connects Pico W to Wi-Fi.
- Disables Wi-Fi power save.
- Listens on UDP port `50222`.
- Parses Tempest `obs_st` packets.
- Performs sanity validation.
- Rate-limits forwarding to once per 60 seconds.
- Sends compact JSON over UART to the RAK/Meshtastic node.
- Sends periodic debug heartbeats every 15 minutes.
- Performs staged recovery when no UDP has been seen for too long.

### Weather fields the Pico currently emits

The current weather payload contains more than the home parser uses:

- `i` message id
- `ts` source timestamp
- `t` temperature
- `h` humidity
- `p` pressure
- `w` average wind
- `g` wind gust
- `l` wind lull
- `d` wind direction
- `r` rain interval
- `uv`
- `sr` solar radiation
- `lux`
- `bat`
- `ld` lightning distance
- `lc` lightning count
- `pt` precipitation type
- `ri` report interval
- `rd` local day rain
- `nr` nearcast rain
- `nrd` local day nearcast rain
- `pa` precipitation analysis type

### Debug heartbeat shape

The health/debug payload includes:

- `sys`
- `i`
- `up`
- `ip`
- `wc`
- `pm`
- `udp`
- `jerr`
- `nobs`
- `rej`
- `skip`
- `sockrec`
- `wifirec`
- `sockerr`
- `nwu`
- `last_udp_s`
- `last_obs_s`
- `last_ok_s`

The home parser currently only normalizes `sys`, `i`, `up`, `ip`, `err`, and `ts`, so the other debug values are effectively ignored unless the missing storage layer preserves raw payloads for later inspection.

## 3.6 `mock_tempest_udp_sender.py`: local test utility

This script generates fake but plausible Tempest `obs_st` packets and sends them over UDP.

Why it matters:

- It lets you test the Pico bridge without real weather hardware.
- It confirms the exact upstream packet shape the Pico expects.
- It provides a reproducible path for bench testing the entire ingest pipeline.

---

# 4. SQLite database: how it works

## 4.1 Why SQLite is used here

SQLite is the home-side durable store and queue. That means it is doing three jobs at once:

1. **Primary local record of accepted weather readings**
2. **Retry queue for AWS delivery**
3. **Operational telemetry store for health/status and rejected packets**

This is a good MVP choice because it is:

- simple to deploy
- durable on disk
- fast enough for tiny traffic volume
- transactional
- easy to inspect manually

## 4.2 Database-wide settings from `schema.sql`

Before tables are created, the schema sets:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

### What those do

- **WAL mode**:
  - SQLite writes go to a write-ahead log first.
  - This improves concurrency for service apps.
  - Readers are less likely to block writers.

- **Foreign keys ON**:
  - SQLite will enforce referential integrity.
  - For example, a queue row cannot legally point at a weather reading that does not exist.

---

# 5. Detailed SQLite schema reference

## 5.1 `weather_readings`

### Purpose

This is the main table for accepted weather observations.

Each row represents one weather reading received from a Meshtastic source node and accepted by the parser/storage layer.

### DDL

```sql
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
    raw_payload TEXT NOT NULL,
    payload_hash TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_node_id, source_ts_utc)
);
```

### Column-by-column explanation

| Column | Type | Meaning |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal SQLite surrogate key. Used by other tables, especially the delivery queue. |
| `source_node_id` | `TEXT NOT NULL` | Meshtastic node id of the sender, such as `!5c32d4d9`. This identifies the station/source. |
| `source_name` | `TEXT` | Optional human-readable sender name if Meshtastic metadata provided one. |
| `msg_id` | `INTEGER NOT NULL` | Message id generated on the garden side. Used for tracing and possibly dedupe logic. |
| `source_ts_utc` | `TEXT NOT NULL` | Timestamp from the originating source payload, not when the Pi received it. In the current flow this usually comes from the Pico's `ts` field and may be an epoch string. |
| `received_at_utc` | `TEXT NOT NULL` | When the home server received and processed the packet. |
| `temp_c` | `REAL NOT NULL` | Air temperature in Celsius. |
| `humidity_pct` | `REAL NOT NULL` | Relative humidity percentage. |
| `pressure_hpa` | `REAL NOT NULL` | Station pressure in hectopascals. |
| `wind_ms` | `REAL NOT NULL` | Wind speed in meters per second. In the current parser this is average wind. |
| `wind_dir_deg` | `INTEGER NOT NULL` | Wind direction in degrees, 0-360. |
| `rain_mm` | `REAL NOT NULL` | Rain amount in millimeters for the interval represented by the packet. |
| `raw_payload` | `TEXT NOT NULL` | Original JSON payload string as received over Meshtastic. Very useful for debugging and future reprocessing. |
| `payload_hash` | `TEXT` | SHA-256 hash of the raw payload. Can support dedupe, diagnostics, or tamper checking. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When SQLite inserted the row locally. |

### Uniqueness rule

```sql
UNIQUE(source_node_id, source_ts_utc)
```

This is important: the actual schema dedupes on **source node + source timestamp**, not source node + message id.

That differs from the design doc, which proposed deduping by `(source_node_id, msg_id)`.

### Likely meaning of that choice

This suggests the current MVP is treating the source timestamp as the true natural key for a weather observation. That works if the garden source emits one reading per source timestamp.

Potential downside:

- if two distinct messages accidentally share the same source timestamp, one could be considered a duplicate.

### Indexes on this table

```sql
CREATE INDEX IF NOT EXISTS idx_weather_received_at
ON weather_readings(received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_received
ON weather_readings(source_node_id, received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_msg
ON weather_readings(source_node_id, msg_id);
```

#### What each index is for

- `idx_weather_received_at`: efficient recent-history scans by receive time
- `idx_weather_source_received`: efficient per-station history lookups
- `idx_weather_source_msg`: efficient lookups by station + message id for debugging or dedupe helpers

## 5.2 `aws_delivery_queue`

### Purpose

This table tracks whether each saved weather reading has been delivered to AWS.

It is the backbone of the at-least-once delivery model.

### DDL

```sql
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
```

### Column-by-column explanation

| Column | Type | Meaning |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal queue row id. This is what `queue_worker.py` logs as `queue_id`. |
| `reading_id` | `INTEGER NOT NULL` | Foreign key to `weather_readings.id`. Each queue row corresponds to one accepted weather reading. |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | Delivery state. In practice this is likely values such as `pending`, `delivered`, and possibly retry/failure states depending on `storage.py`. |
| `attempt_count` | `INTEGER NOT NULL DEFAULT 0` | Number of AWS delivery attempts recorded so far. |
| `next_attempt_at_utc` | `TEXT` | Earliest time a failed row should be retried. This supports deferred retry scheduling. |
| `last_attempt_at_utc` | `TEXT` | Timestamp of the most recent AWS delivery attempt. |
| `delivered_at_utc` | `TEXT` | Timestamp when AWS delivery succeeded. |
| `last_error` | `TEXT` | Most recent error message from an AWS delivery failure. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When the queue row was created. Usually immediately after weather row insertion. |
| `updated_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | Last time the queue row was changed. |

### Constraints and behavior

- `FOREIGN KEY(reading_id) ... ON DELETE CASCADE`:
  - deleting a weather reading will automatically delete its queue entry
- `UNIQUE(reading_id)`:
  - one weather reading can only have one queue row

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_delivery_status_next_attempt
ON aws_delivery_queue(status, next_attempt_at_utc, id);
```

This is designed for the worker query pattern:

- find rows with a deliverable status
- whose `next_attempt_at_utc` is due
- process them in a stable order

### Likely storage-layer behavior

Because `queue_worker.py` calls `fetch_pending_deliveries(limit=10)`, the missing storage layer likely does something like:

- join `aws_delivery_queue` to `weather_readings`
- return rows where `status = 'pending'`
- and `next_attempt_at_utc` is null or due
- ordered by queue id or time

## 5.3 `device_health_events`

### Purpose

Stores inbound health/debug/status packets from the garden side.

This is operational telemetry, not business weather data.

### DDL

```sql
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
```

### Column-by-column explanation

| Column | Type | Meaning |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal row id. |
| `source_node_id` | `TEXT NOT NULL` | Meshtastic sender id for the device that emitted the health packet. |
| `source_name` | `TEXT` | Optional sender name. |
| `msg_id` | `INTEGER` | Optional health message id. |
| `source_ts_utc` | `TEXT` | Optional timestamp from the originating device payload. |
| `received_at_utc` | `TEXT NOT NULL` | When the Pi received the health packet. |
| `status` | `TEXT NOT NULL` | Health/status string from the `sys` field, such as `ok`, `warn`, `error`, or in the current Pico debug build, `dbg`. |
| `uptime_sec` | `INTEGER` | Device uptime in seconds if present. |
| `ip_address` | `TEXT` | Device IP address reported by the garden-side sender if present. |
| `error_reason` | `TEXT` | Optional error detail from the `err` field. |
| `raw_payload` | `TEXT NOT NULL` | Original JSON health packet. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | Insert time in SQLite. |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_health_source_received
ON device_health_events(source_node_id, received_at_utc);
```

This supports per-device health history and “latest health” lookups.

## 5.4 `device_status_current`

### Purpose

This is the latest-known state snapshot for each source node.

Instead of scanning all event history each time, the app can update one current-state row per source.

### DDL

```sql
CREATE TABLE IF NOT EXISTS device_status_current (
    source_node_id TEXT PRIMARY KEY,
    source_name TEXT,
    last_weather_at_utc TEXT,
    last_health_at_utc TEXT,
    last_status TEXT,
    derived_state TEXT,
    last_msg_id INTEGER,
    last_source_ts_utc TEXT,
    last_uptime_sec INTEGER,
    last_ip_address TEXT,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### Column-by-column explanation

| Column | Type | Meaning |
|---|---|---|
| `source_node_id` | `TEXT PRIMARY KEY` | One row per device/source node. This is the key of the current-status snapshot. |
| `source_name` | `TEXT` | Optional human-readable node name. |
| `last_weather_at_utc` | `TEXT` | Timestamp of the latest accepted weather reading for this source. |
| `last_health_at_utc` | `TEXT` | Timestamp of the latest health packet for this source. |
| `last_status` | `TEXT` | Raw latest health status string, e.g. `ok`, `dbg`, `warn`, `error`. |
| `derived_state` | `TEXT` | Computed operational state such as `healthy`, `degraded`, `offline`, or `bad_health`. |
| `last_msg_id` | `INTEGER` | Latest seen message id, probably from either health or weather depending on storage logic. |
| `last_source_ts_utc` | `TEXT` | Latest source-side timestamp seen from the device. |
| `last_uptime_sec` | `INTEGER` | Latest reported uptime in seconds. |
| `last_ip_address` | `TEXT` | Latest reported IP address. |
| `updated_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When this current-state row was last updated. |

### How it likely works

Although the storage implementation is missing, the natural behavior would be:

- on weather insert: update `last_weather_at_utc`, maybe `last_msg_id`, maybe `last_source_ts_utc`
- on health insert: update `last_health_at_utc`, `last_status`, `last_uptime_sec`, `last_ip_address`
- compute `derived_state` based on freshness and status

This matches the design spec even though the exact update function is not visible.

## 5.5 `ingest_events`

### Purpose

Stores packets that were seen by the listener but **not** accepted as valid weather rows or health rows.

This is a diagnostics table.

### DDL

```sql
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
```

### Column-by-column explanation

| Column | Type | Meaning |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal event row id. |
| `received_at_utc` | `TEXT NOT NULL` | When the home server saw the packet. |
| `source_node_id` | `TEXT` | Source node id if known. |
| `source_name` | `TEXT` | Source node name if known. |
| `packet_type` | `TEXT NOT NULL` | Parser classification such as `invalid`, `rejected`, `unknown`, or possibly duplicate-related bookkeeping. |
| `reason` | `TEXT` | Why it was rejected or categorized, such as `malformed_json` or `unrecognized_payload_shape`. |
| `msg_id` | `INTEGER` | Message id if one could be extracted. |
| `raw_payload` | `TEXT` | Original text payload for debugging. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | Insert time. |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_ingest_received
ON ingest_events(received_at_utc);
```

This supports recent-error inspection and troubleshooting timelines.

---

# 6. How the SQLite pieces work together

## 6.1 Valid weather packet path

Likely flow:

```text
listener receives TEXT_MESSAGE_APP
  -> parser classifies packet as weather
  -> storage inserts row into weather_readings
  -> storage inserts/ensures one row in aws_delivery_queue
  -> storage updates device_status_current
```

## 6.2 Duplicate weather packet path

Likely flow:

```text
listener receives weather packet
  -> storage detects duplicate based on schema uniqueness or helper logic
  -> no second weather row inserted
  -> listener records ingest event for visibility
```

## 6.3 Health packet path

Likely flow:

```text
listener receives health packet
  -> parser classifies as health
  -> storage inserts into device_health_events
  -> storage updates device_status_current
```

## 6.4 Invalid / unknown packet path

Likely flow:

```text
listener receives packet
  -> parser returns invalid / rejected / unknown
  -> no weather row inserted
  -> no queue row inserted
  -> ingest_events row inserted for diagnostics
```

## 6.5 Successful AWS delivery path

Likely flow:

```text
queue worker fetches pending row
  -> POST to AWS succeeds
  -> mark_delivery_success(queue_id)
  -> queue row status becomes delivered
  -> delivered_at_utc populated
```

## 6.6 Failed AWS delivery path

Likely flow:

```text
queue worker fetches pending row
  -> POST fails
  -> mark_delivery_failure(queue_id, error)
  -> attempt_count increments
  -> last_error updated
  -> next_attempt_at_utc scheduled
  -> row remains retryable
```

---

# 7. AWS infrastructure: how the cloud side is built

The AWS stack is defined in two YAML files:

- `weather-station-stack.yaml` = source CloudFormation template
- `packaged-template.yaml` = deployment-ready packaged version whose Lambda code references S3 objects

## 7.1 High-level AWS architecture

```text
API Gateway HTTP API
  -> POST /observations
       -> ingest Lambda (`app.py`)
       -> DynamoDB table

  -> GET /observations/latest
       -> read Lambda (`appRead.py`)
       -> DynamoDB table

  -> GET /observations
       -> read Lambda (`appRead.py`)
       -> DynamoDB table
```

## 7.2 CloudFormation parameters

### `ProjectName`
Default: `weather-station`

Used to build resource names.

### `EnvironmentName`
Default: `dev`

Lets the same template create different environments such as dev/test/prod.

### `ApiSharedSecret`
- required secret
- `NoEcho: true`
- minimum length 16

This becomes the shared credential expected in the `x-weatherstation-key` header.

### `TableName`
Default: `WeatherObservations`

Controls the DynamoDB table name.

### `AllowedCorsOrigin`
Default: `*`

Controls CORS headers emitted by the API/Lambdas.

---

# 8. AWS resources, one by one

## 8.1 `WeatherObservationsTable` (DynamoDB)

### Table type

```yaml
Type: AWS::DynamoDB::Table
BillingMode: PAY_PER_REQUEST
```

This is a good fit for low, bursty usage because you do not need to provision capacity up front.

### Key schema

- partition key: `pk` (string)
- sort key: `sk` (string)

This is a **single-table design**. Different logical record types share one table and are distinguished by key patterns and `record_type` attributes.

## 8.2 `WeatherLambdaRole` (IAM role)

The Lambda role allows:

### DynamoDB permissions

- `dynamodb:GetItem`
- `dynamodb:PutItem`
- `dynamodb:Query`
- `dynamodb:TransactWriteItems`

### CloudWatch Logs permissions

- `logs:CreateLogGroup`
- `logs:CreateLogStream`
- `logs:PutLogEvents`

This is close to least privilege for the MVP.

## 8.3 `IngestObservationFunction`

### Runtime settings

- runtime: `python3.13`
- handler: `app.handler`
- timeout: 10 seconds
- memory: 256 MB
- architecture: `arm64`

### Environment variables

- `TABLE_NAME`
- `API_SHARED_SECRET`
- `ALLOWED_CORS_ORIGIN`

### Code source

In the source template:

```yaml
Code: ingest/
```

In the packaged template:

- S3 bucket = `brahm-weather-station-cfn-artifacts-2026`
- S3 key = packaged artifact id

## 8.4 `ReadObservationFunction`

Similar runtime settings, but environment contains:

- `TABLE_NAME`
- `ALLOWED_CORS_ORIGIN`

No shared secret is needed because the current read routes are not authenticated in the Lambda code.

## 8.5 `WeatherHttpApi`

This is an API Gateway **HTTP API** with CORS configured to allow:

- methods: `GET`, `POST`, `OPTIONS`
- headers: `content-type`, `x-weatherstation-key`

## 8.6 Integrations and routes

### Routes

- `POST /observations` -> ingest Lambda
- `GET /observations/latest` -> read Lambda
- `GET /observations` -> read Lambda

### Stage

- `$default`
- `AutoDeploy: true`

That means the API is available without an explicit stage name in the URL path.

## 8.7 Lambda invoke permissions

Two `AWS::Lambda::Permission` resources allow API Gateway to invoke the Lambdas.

## 8.8 Outputs

The stack exports:

- `ApiBaseUrl`
- `TableNameOutput`
- `IngestFunctionNameOutput`
- `ReadFunctionNameOutput`

---

# 9. DynamoDB schema: how the cloud database works

DynamoDB is not relational, so it does not have fixed SQL columns in the same way SQLite does. Instead, it stores **items with attributes**.

In this MVP, there is one table that stores several logical item types.

## 9.1 Table keys

### `pk`
Partition key.

Pattern:

```text
STATION#<source_node_id>
```

Example:

```text
STATION#!5c32d4d9
```

This groups all items for one station together.

### `sk`
Sort key.

There are three main patterns:

1. observation item
   ```text
   OBS#<received_at_utc>#<padded_msg_id>
   ```
2. latest item
   ```text
   LATEST
   ```
3. dedupe item
   ```text
   DEDUPE#<padded_msg_id>
   ```

### Why the sort key format matters

Because `received_at_utc` is an ISO UTC timestamp string, lexical sort order matches chronological order. That makes history queries simple.

The padded message id ensures stable ordering even if two observations share the same timestamp string.

---

# 10. DynamoDB item types in detail

## 10.1 Observation item

Created for every accepted cloud ingest.

### Key pattern

- `pk = STATION#<source_node_id>`
- `sk = OBS#<received_at_utc>#<zero-padded-msg-id>`

### Attributes stored by the ingest Lambda

| Attribute | Meaning |
|---|---|
| `pk` | Station partition key |
| `sk` | Observation sort key |
| `record_type` | Literal value `observation` |
| `source_node_id` | Station/source node id |
| `source_name` | Optional node name |
| `msg_id` | Integer message id |
| `source_ts_utc` | Source timestamp from the payload |
| `received_at_utc` | Home server receive timestamp |
| `weather` | Nested object of weather fields |
| `raw_payload` | Entire request body that Lambda received |
| `ingested_at_utc` | When Lambda stored the record |

### Role of this item type

This is the durable historical record used for history queries.

## 10.2 Latest item

A rolling snapshot of the newest known observation for the station.

### Key pattern

- `pk = STATION#<source_node_id>`
- `sk = LATEST`

### Attributes stored

| Attribute | Meaning |
|---|---|
| `pk` | Station partition key |
| `sk` | Literal `LATEST` |
| `record_type` | Literal `latest` |
| `source_node_id` | Station id |
| `source_name` | Optional station name |
| `msg_id` | Message id of the newest reading |
| `source_ts_utc` | Source timestamp of newest reading |
| `received_at_utc` | Home receive time of newest reading |
| `weather` | Nested weather object for newest reading |
| `latest_observation_sk` | Pointer to the observation item sort key |
| `updated_at_utc` | When this latest snapshot was refreshed |

### Role of this item type

This makes `GET /observations/latest` a fast point lookup.

## 10.3 Dedupe item

Used to prevent duplicate writes for the same station/message-id combination.

### Key pattern

- `pk = STATION#<source_node_id>`
- `sk = DEDUPE#<zero-padded-msg-id>`

### Attributes stored

| Attribute | Meaning |
|---|---|
| `pk` | Station partition key |
| `sk` | Dedupe key for a message id |
| `record_type` | Literal `dedupe` |
| `source_node_id` | Station id |
| `msg_id` | Integer message id |
| `received_at_utc` | Receive time associated with the dedupe marker |

### Role of this item type

The Lambda writes this item first in a DynamoDB transaction with a condition expression:

```text
attribute_not_exists(pk) AND attribute_not_exists(sk)
```

If the dedupe item already exists, the transaction is canceled and the Lambda returns:

```json
{
  "ok": true,
  "deduped": true
}
```

That is the cloud-side idempotency guarantee.

---

# 11. AWS ingest Lambda (`app.py`): detailed behavior

## 11.1 Request contract

The Lambda only accepts:

- method: `POST`
- path: `/observations`

Anything else returns 404.

## 11.2 Authentication

The request must include:

```http
x-weatherstation-key: <shared secret>
```

If the secret does not match `API_SHARED_SECRET`, the Lambda returns `401 unauthorized`.

## 11.3 Body parsing

- JSON request body is parsed.
- If `isBase64Encoded` is true, body is base64-decoded first.
- Floats are parsed as `Decimal` for accuracy.

## 11.4 Payload validation

The Lambda expects:

```json
{
  "payload": {
    "source_node_id": "...",
    "msg_id": 123,
    "received_at_utc": "2026-03-18T00:45:46.982324Z",
    "source_name": "optional",
    "source_ts_utc": "optional",
    "weather": { ... }
  }
}
```

### Required fields

- `payload.source_node_id` must be a string
- `payload.msg_id` must exist and be integer-convertible
- `payload.received_at_utc` must be ISO-8601 UTC string ending in `Z`

### Optional source timestamp rules

`source_ts_utc` may be:

- epoch string of digits
- ISO-8601 UTC string
- integer / Decimal epoch

### Weather validation rules

The ingest Lambda is richer than the current home parser. It understands many optional fields, including:

- `wind_lull_ms`
- `wind_avg_ms`
- `wind_gust_ms`
- `wind_dir_deg`
- `wind_sample_interval_s`
- `station_pressure_hpa`
- `air_temp_c`
- `relative_humidity_pct`
- `illuminance_lux`
- `uv_index`
- `solar_radiation_wm2`
- `rain_interval_mm`
- `precipitation_type`
- `lightning_avg_distance_km`
- `lightning_strike_count`
- `battery_voltage_v`
- `report_interval_min`
- `local_day_rain_mm`
- `nearcast_rain_mm`
- `local_day_nearcast_rain_mm`
- `precipitation_analysis_type`
- `timestamp`

So the cloud side is already designed to accept a fuller weather model than the current home parser is persisting.

## 11.5 Transaction write behavior

The Lambda uses one `transact_write_items` call containing three writes:

1. Put dedupe item with `attribute_not_exists` guard
2. Put observation item
3. Put latest item

This guarantees that:

- you do not get a latest item without an observation
- you do not get an observation without its dedupe marker
- duplicate message ids for a station are safely absorbed

---

# 12. AWS read Lambda (`appRead.py`): detailed behavior

## 12.1 Supported routes

### `GET /observations/latest`

Required query string:

- `stationId`

Behavior:

- builds `pk = STATION#<stationId>`
- gets the item with `sk = LATEST`
- returns 404 if not found

### `GET /observations`

Required query string:

- `stationId`
- `from`
- `to`

Optional:

- `limit` (default 200, max 1000)

Behavior:

- builds `pk = STATION#<stationId>`
- queries sort key range:
  - from `OBS#<from>`
  - to `OBS#<to>~`
- returns newest-first because `ScanIndexForward=False`

The `~` suffix is a common lexical trick so all keys beginning with the `to` timestamp range are included.

## 12.2 Response shape

The read Lambda deserializes DynamoDB attribute values and returns normal JSON.

For history it returns:

```json
{
  "ok": true,
  "count": <n>,
  "items": [ ... ]
}
```

For latest it returns:

```json
{
  "ok": true,
  "item": { ... }
}
```

---

# 13. Where the current MVP is ahead of the original design

The provided implementation is ahead of the older design docs in a few places.

## 13.1 Richer cloud weather schema

The AWS ingest Lambda already validates many more weather fields than the local home parser currently stores.

## 13.2 Separate worker process instead of one threaded process

The design spec described a single service with a background worker thread. The actual MVP appears to be split into at least two scripts/processes:

- `listen_meshtastic.py`
- `queue_worker.py`

That is still a valid architecture and may actually be easier to supervise and debug.

## 13.3 Native systemd notify support

`queue_worker.py` already includes systemd notify/watchdog support, which is more production-ready than the earlier design description.

---

# 14. Where the current MVP differs from the design docs

These differences are important for future maintenance.

## 14.1 SQLite dedupe key changed

Design doc expectation:

- unique by `(source_node_id, msg_id)`

Actual schema:

- unique by `(source_node_id, source_ts_utc)`

## 14.2 SQLite path config changed

Design doc expectation:

- path configurable via environment

Actual uploaded `db.py`:

- path is hardcoded under `~/weatherstation-home/weatherstation/weatherstation.db`

## 14.3 Environment variable names changed

Design doc expectation:

- `AWS_API_URL`
- `AWS_API_KEY`

Actual `queue_worker.py`:

- `API_URL`
- `API_KEY`

## 14.4 Health heartbeat cadence changed

Design doc suggested a 6-hour advisory heartbeat.

Current Pico code sends debug heartbeats every **15 minutes** with `sys = "dbg"`.

## 14.5 Rich weather fields not yet persisted locally

The Pico and AWS sides understand more weather fields than the local parser/schema currently persists.

---

# 15. Inferred storage-layer contract (`storage.py`)

Because the file is missing, the best way to explain it is by its implied API.

## 15.1 `insert_weather(parsed)`
Likely responsibilities:

- insert into `weather_readings`
- compute/store `payload_hash`
- create one `aws_delivery_queue` row
- update `device_status_current`
- return something like `"duplicate"` when uniqueness prevents insertion

## 15.2 `insert_health(parsed)`
Likely responsibilities:

- insert into `device_health_events`
- update `device_status_current`
- maybe recompute `derived_state`

## 15.3 `record_ingest_event(parsed)`
Likely responsibilities:

- insert non-primary packets into `ingest_events`
- preserve `packet_type`, `reason`, `raw_payload`, source info, and `msg_id`

## 15.4 `fetch_pending_deliveries(limit=10)`
Likely responsibilities:

- query due rows from `aws_delivery_queue`
- join them with `weather_readings`
- return the combined row data that `queue_worker.py` expects

The worker expects fields such as:

- `queue_id`
- `reading_id`
- `attempt_count`
- `source_node_id`
- `source_name`
- `msg_id`
- `source_ts_utc`
- `received_at_utc`
- weather columns

## 15.5 `mark_delivery_success(queue_id)`
Likely responsibilities:

- set `status = delivered`
- set `delivered_at_utc`
- update `updated_at_utc`

## 15.6 `mark_delivery_failure(queue_id, error)`
Likely responsibilities:

- increment `attempt_count`
- write `last_error`
- write `last_attempt_at_utc`
- compute `next_attempt_at_utc`
- keep row eligible for future retry

---

# 16. Practical interpretation: how both databases relate to each other

## SQLite role

SQLite is the **operational edge database**.

It is responsible for:

- durability on the Raspberry Pi
- temporary disconnection tolerance
- dedupe / queue control at the edge
- health and ingest diagnostics

Think of SQLite as the **authoritative local backlog and event journal**.

## DynamoDB role

DynamoDB is the **cloud serving database**.

It is responsible for:

- storing historical observations for the website/API
- returning the latest observation efficiently
- deduping repeated uploads from the home server

Think of DynamoDB as the **cloud publication layer**.

## Why the split is good

This split keeps internet failures from affecting local capture.

Even if AWS is down:

- Meshtastic ingest can continue
- SQLite can keep accepting rows
- the queue can drain later when the network returns

---

# 17. Suggested follow-up improvements

Based on the current code, these are the highest-value next improvements.

## 17.1 Add the missing `storage.py` to the repo/docs bundle

This is the most important documentation gap.

## 17.2 Align dedupe strategy across edge and cloud

Decide whether the canonical observation identity is:

- `(source_node_id, msg_id)`
- or `(source_node_id, source_ts_utc)`

Right now SQLite and DynamoDB are using different natural keys.

## 17.3 Persist the richer weather fields locally

The Pico already produces them and AWS already accepts them.

The missing link is the local parser/schema/storage layer.

## 17.4 Formalize systemd units

The current design suggests at least two supervised services:

- ingest listener service
- queue worker service

## 17.5 Make DB path and API config fully environment-driven

That would bring the implementation back into line with the design spec.

---

# 18. Bottom line

The MVP is structurally sound.

It already has the key production-minded patterns in place:

- edge persistence
- decoupled delivery queue
- retry with backoff
- cloud-side dedupe
- a latest-item pattern for fast reads
- raw-payload retention for debugging

The biggest current documentation/code gap is the missing `storage.py` file. Other than that, the architecture is coherent:

- **Pico produces weather JSON**
- **Pi listener validates and stores it in SQLite**
- **queue worker drains SQLite to AWS**
- **AWS writes history + latest + dedupe items into DynamoDB**
- **read Lambda serves latest and history to downstream clients**

