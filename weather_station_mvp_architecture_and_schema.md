# Weather Station MVP Architecture and Schema Guide

Updated after direct review of `storage.py`.

This document explains the current working MVP as represented by the uploaded code and templates. It focuses on three things:

1. the **SQLite database schema** on the home Raspberry Pi,
2. the **home server code path** that ingests Meshtastic packets and forwards them to AWS, and
3. the **AWS schema and infrastructure** used to accept, store, deduplicate, and read observations.

---

## Files reviewed

### Design and requirements
- `weather_station_ai_handoff.docx`
- `home_pi_server_design_spec.docx`
- `weather_station_design_revised_v2 (2).docx`

### Home-side Python code
- `listen_meshtastic.py`
- `parser.py`
- `db.py`
- `schema.sql`
- `storage.py`
- `queue_worker.py`

### Garden-side producer and test utilities
- `main.py`
- `mock_tempest_udp_sender.py`

### AWS infrastructure and Lambda code
- `weather-station-stack.yaml`
- `packaged-template.yaml`
- `app.py`
- `appRead.py`

---

## 1. MVP in one pass

The system is a store-and-forward pipeline.

```text
Tempest sensor
  -> Tempest hub
  -> UDP broadcast on local LAN
  -> Pico W bridge
  -> compact JSON over UART
  -> garden Meshtastic node
  -> LoRa / Meshtastic
  -> home Meshtastic node
  -> USB serial to Raspberry Pi
  -> listen_meshtastic.py
  -> parser.py
  -> storage.py + SQLite
  -> queue_worker.py
  -> AWS HTTP API
  -> ingest Lambda
  -> DynamoDB
```

A second read path exists for the website or any viewer:

```text
Viewer / website
  -> GET /observations/latest or GET /observations
  -> API Gateway
  -> read Lambda
  -> DynamoDB
```

The most important architectural rule in the MVP is this:

- **SQLite is the local system of record.**
- **AWS is the downstream serving layer.**
- **Weather is saved locally first, then delivered to AWS later.**

That design is what gives the system resilience when the internet or AWS is unavailable.

---

## 2. Home-side runtime architecture

There are really two long-running home-side processes.

### Process 1: `listen_meshtastic.py`
This is the ingest daemon.

Its job is to:
- connect to the home Meshtastic node over USB serial,
- receive inbound packets,
- extract the text payload,
- parse and classify the payload,
- write valid data into SQLite.

### Process 2: `queue_worker.py`
This is the cloud delivery daemon.

Its job is to:
- poll SQLite for readings that have not yet been delivered,
- transform them into the AWS API payload shape,
- POST them to the AWS API,
- mark success or schedule retry in SQLite.

Together they implement a durable pipeline:

```text
Meshtastic packet received
  -> saved locally
  -> queued for delivery
  -> retried until AWS accepts it
```

---

## 3. Home-side code, file by file

### 3.1 `listen_meshtastic.py`

This script is the entry point for home-side ingestion.

#### What it does
- Builds a base path under `~/weatherstation-home`.
- Adds `~/weatherstation-home/weatherstation` to `sys.path`.
- Imports:
  - `parse_text_payload` from `parser.py`
  - `insert_health`, `insert_weather`, and `record_ingest_event` from `storage.py`
- Opens a Meshtastic serial connection using `meshtastic.serial_interface.SerialInterface`.
- Subscribes to pubsub events:
  - `meshtastic.receive`
  - `meshtastic.connection.established`
  - `meshtastic.connection.lost`
- Reconnects in a loop if the serial connection drops.

#### What packets it actually processes
It logs all received packets, but only parses packets when:

- `decoded.portnum == "TEXT_MESSAGE_APP"`

If the packet is `TELEMETRY_APP`, it logs that it saw it, but does not try to store it as weather.

#### How payload text is extracted
The script first looks for:
- `decoded["text"]`

If that is absent, it falls back to:
- `decoded["payload"]`

and decodes bytes to UTF-8 if needed.

#### How source metadata is captured
It extracts:
- `fromId` as `source_node_id`
- `user.longName` or `user.shortName` as `source_name`

That means the SQLite records can carry both a stable node id and a friendly node name when Meshtastic exposes one.

#### Processing flow
Once a text payload is extracted, the flow is:

```text
process_text_packet()
  -> parse_text_payload(...)
  -> if packet_type == weather:
       insert_weather(parsed)
       if duplicate:
         record_ingest_event(parsed)
     elif packet_type == health:
       insert_health(parsed)
     else:
       record_ingest_event(parsed)
```

#### Important behavior
- Empty text payloads are skipped and logged.
- Parse exceptions are caught and logged.
- Storage exceptions are caught and logged.
- The service is designed to keep running rather than crash on bad packets.

---

### 3.2 `parser.py`

This file is where raw text becomes structured events.

#### Core datatype: `ParsedEvent`
The parser returns a `ParsedEvent` dataclass with these fields:

- `packet_type`
- `reason`
- `source_node_id`
- `source_name`
- `msg_id`
- `received_at_utc`
- `source_ts_utc`
- `raw_payload`
- `normalized`

This structure is used by `storage.py` for inserts.

#### Supported packet types

##### `weather`
A payload is classified as weather when it contains all of these keys:
- `i`
- `t`
- `h`
- `p`
- `w`
- `d`
- `r`

Those are normalized into:
- `msg_id`
- `temp_c`
- `humidity_pct`
- `pressure_hpa`
- `wind_ms`
- `wind_dir_deg`
- `rain_mm`
- `source_ts_utc`

Validation ranges enforced by the parser are:
- temperature: `-60` to `70`
- humidity: `0` to `100`
- pressure: `800` to `1200`
- wind: `0` to `100`
- wind direction: `0` to `360`
- rain: `0` to `500`

If conversion or validation fails, the parser returns `packet_type="rejected"`.

##### `health`
A payload is classified as health when it contains `sys`.

Normalized health fields are:
- `status`
- `msg_id`
- `uptime_sec`
- `ip_address`
- `error_reason`
- `source_ts_utc`

If health parsing fails, the parser returns `packet_type="invalid"` with a reason beginning `bad_health_payload:`.

##### `invalid`
Used when:
- JSON cannot be parsed,
- JSON is not an object,
- health parsing fails.

##### `rejected`
Used when the payload looks like weather, but numeric conversion or range validation fails.

##### `unknown`
Used when the JSON is valid, but does not match either the weather or health contract.

#### Important implementation detail
The parser currently recognizes only the **core compact weather fields**. The richer Pico payload also includes fields like gust, lull, UV, solar radiation, illuminance, lightning, battery, and precipitation analysis fields, but those are not normalized into `ParsedEvent.normalized` for weather. In the current MVP, those extra values are dropped at the parser boundary even though the Pico sends them.

---

### 3.3 `db.py`

This file defines how SQLite connections are opened.

#### Database location
The database path is hardcoded as:

```text
~/weatherstation-home/weatherstation/weatherstation.db
```

#### Connection behavior
Each connection:
- uses `sqlite3.Row` as the row factory,
- enables `PRAGMA foreign_keys=ON`,
- enables `PRAGMA journal_mode=WAL`.

#### Why that matters
- `sqlite3.Row` allows code to access columns by name instead of numeric index.
- `foreign_keys=ON` enforces integrity between `aws_delivery_queue` and `weather_readings`.
- `WAL` mode is a strong fit for a small service app with concurrent reads and writes.

#### Important note
The design docs mention an environment-configurable path, but the current MVP code does **not** use one. The path is fixed in code.

---

### 3.4 `storage.py`

This file is the heart of the local persistence layer. It is the most important bridge between parsed events and the SQLite schema.

It provides the following functions:
- `record_ingest_event()`
- `insert_weather()`
- `insert_health()`
- `fetch_pending_deliveries()`
- `mark_delivery_success()`
- `mark_delivery_failure()`

It also defines:
- `utc_now()`
- `compute_next_attempt()`

#### 3.4.1 `record_ingest_event(event, packet_type_override=None, reason_override=None)`

This writes a row into the `ingest_events` table.

It stores:
- `received_at_utc`
- `source_node_id`
- `source_name`
- `packet_type`
- `reason`
- `msg_id`
- `raw_payload`

This is used for packets that were seen but not stored as weather or health, and also for duplicates.

#### 3.4.2 `insert_weather(event)`

This function inserts accepted weather into the local durable store and queues it for AWS delivery.

##### What it requires
It requires `event.normalized` to exist and it requires `source_ts_utc` to be present and non-empty.

If `source_ts_utc` is missing, it raises:
- `ValueError("weather_missing_source_ts_utc")`

That means the current home-side pipeline will **not** store weather rows that do not contain a source timestamp.

##### Insert behavior
It inserts a row into `weather_readings` with:
- `source_node_id`
- `source_name`
- `msg_id`
- `source_ts_utc`
- `received_at_utc`
- `temp_c`
- `humidity_pct`
- `pressure_hpa`
- `wind_ms`
- `wind_dir_deg`
- `rain_mm`
- `raw_payload`
- `payload_hash`

The payload hash is computed with SHA-256 over the raw payload string.

##### Duplicate behavior
If the insert triggers `sqlite3.IntegrityError`, `insert_weather()` returns:
- `"duplicate"`

It does **not** raise the error up as fatal.

The listener then logs and records the duplicate as an ingest event.

##### Queue behavior
If the weather row is inserted successfully, `insert_weather()` immediately inserts a row into:
- `aws_delivery_queue`

with:
- `reading_id`
- `status = 'pending'`

##### Current-status behavior
It also upserts into `device_status_current`, setting:
- `source_node_id`
- `source_name`
- `last_weather_at_utc`
- `last_msg_id`
- `last_source_ts_utc`
- `updated_at_utc`

It does **not** compute `derived_state`.

##### Return value
- `"inserted"` on success
- `"duplicate"` on uniqueness conflict

#### 3.4.3 `insert_health(event)`

This inserts a health/status packet into `device_health_events` and updates the current snapshot table.

##### Inserted history fields
It stores:
- `source_node_id`
- `source_name`
- `msg_id`
- `source_ts_utc`
- `received_at_utc`
- `status`
- `uptime_sec`
- `ip_address`
- `error_reason`
- `raw_payload`

##### Current snapshot update
It upserts into `device_status_current`, setting:
- `source_name`
- `last_health_at_utc`
- `last_status`
- `last_msg_id`
- `last_source_ts_utc`
- `last_uptime_sec`
- `last_ip_address`
- `updated_at_utc`

Again, it does **not** compute or update `derived_state`.

#### 3.4.4 `compute_next_attempt(attempt_count)`

This function defines the local SQLite retry schedule.

The schedule is:
- attempt 1 -> retry after **30 seconds**
- attempt 2 -> retry after **120 seconds**
- attempt 3 -> retry after **600 seconds**
- attempt 4 and beyond -> retry after **1800 seconds**

This is the retry schedule written into SQLite when delivery fails.

#### 3.4.5 `fetch_pending_deliveries(limit=10)`

This query joins `aws_delivery_queue` to `weather_readings` and returns rows that are ready to be posted.

It selects rows where:
- `status IN ('pending', 'retry')`
- and `next_attempt_at_utc IS NULL` or already due

It orders them by queue id ascending and limits the result count.

##### Returned columns
The query returns these values:
- `queue_id`
- `reading_id`
- `status`
- `attempt_count`
- `next_attempt_at_utc`
- `source_node_id`
- `source_name`
- `msg_id`
- `source_ts_utc`
- `received_at_utc`
- `temp_c`
- `humidity_pct`
- `pressure_hpa`
- `wind_ms`
- `wind_dir_deg`
- `rain_mm`
- `raw_payload`

This is important because it means the current queue worker only has access to the **basic local weather columns**. Even though `queue_worker.py` knows how to build a richer AWS payload, the current storage query does not provide those richer columns.

#### 3.4.6 `mark_delivery_success(queue_id)`

On success, the queue row is updated to:
- `status = 'delivered'`
- `delivered_at_utc = CURRENT_TIMESTAMP`
- `last_attempt_at_utc = CURRENT_TIMESTAMP`
- `updated_at_utc = CURRENT_TIMESTAMP`
- `last_error = NULL`

#### 3.4.7 `mark_delivery_failure(queue_id, error)`

On failure, the function:
- loads the current `attempt_count`,
- increments it,
- computes `next_attempt_at_utc` using `compute_next_attempt()`,
- truncates the stored error string to 500 chars,
- updates the queue row to:
  - `status = 'retry'`
  - new `attempt_count`
  - new `next_attempt_at_utc`
  - `last_attempt_at_utc = CURRENT_TIMESTAMP`
  - `updated_at_utc = CURRENT_TIMESTAMP`
  - `last_error = truncated error`

There is no terminal `failed` state in the current implementation. A failed row remains retryable forever unless the code or data is changed manually.

---

### 3.5 `queue_worker.py`

This is the outbound AWS delivery worker.

#### Configuration
It loads settings from `.env` or environment variables:
- `API_URL`
- `API_KEY`

The `.env` path is computed as:

```text
<repo parent>/.env
```

#### What it sends
For each pending delivery row it builds:

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

It normalizes `received_at_utc` so that `+00:00` becomes `Z` before sending to AWS.

#### HTTP behavior
It POSTs to:
- `API_URL + "/observations"`

and sends the header:
- `x-weatherstation-key: API_KEY`

#### Retryable errors inside the worker loop
These are considered retryable:
- HTTP `429`
- HTTP `500`
- HTTP `502`
- HTTP `503`
- HTTP `504`
- network errors
- timeouts

Other HTTP errors are treated as non-retryable for that processing attempt, but note the important interaction with `storage.py`:
- `process_one()` catches **all** exceptions,
- then calls `mark_delivery_failure(queue_id, str(exc))`.

So even non-retryable application errors still end up in local queue state as `status='retry'`. The distinction in `queue_worker.py` mainly affects the **in-process immediate retry loop**, not whether the row will ever be retried later by SQLite.

#### Immediate retry schedule inside the worker
Inside a single processing cycle, the worker may retry up to 5 times with exponential backoff:
- 2 seconds
- 4 seconds
- 8 seconds
- 16 seconds
- capped at 30 seconds

That is separate from the longer SQLite retry schedule in `storage.py`.

So there are really **two retry layers**:

1. **Immediate in-memory retry** inside `queue_worker.py`
2. **Deferred persistent retry** via `aws_delivery_queue.next_attempt_at_utc`

#### Systemd support
The worker includes native support for `NOTIFY_SOCKET` and watchdog pings. It can emit:
- `READY=1`
- `STATUS=...`
- `STOPPING=1`
- `WATCHDOG=1`

That makes it suitable for a `Type=notify` systemd unit.

---

### 3.6 `main.py` on the Pico W

This file runs on the Pico and defines what the home server receives.

#### Core behavior
- Connects to Wi-Fi
- Disables Wi-Fi power saving
- Listens on UDP port `50222`
- Accepts Tempest `obs_st` packets
- Validates and rate-limits them
- Sends a compact JSON line over UART
- Sends debug heartbeat packets every 15 minutes
- Rebuilds the socket or reconnects Wi-Fi if no UDP is seen for long enough

#### Weather payload it emits
The Pico emits many more fields than the home parser currently stores:
- `i`
- `ts`
- `t`
- `h`
- `p`
- `w`
- `g`
- `l`
- `d`
- `r`
- `uv`
- `sr`
- `lux`
- `bat`
- `ld`
- `lc`
- `pt`
- `ri`
- `rd`
- `nr`
- `nrd`
- `pa`

#### Health/debug heartbeat
The Pico heartbeat includes:
- `sys`
- `i`
- `up`
- `ip`
- and many extra debug counters

The home parser only normalizes a subset of the heartbeat fields. The raw payload is still stored for health events, so the extra fields are not totally lost, but they are not promoted into dedicated columns.

---

### 3.7 `mock_tempest_udp_sender.py`

This is a local dev/test utility that emits realistic `obs_st` UDP packets.

It exists to test the Pico bridge without requiring the real Tempest hub.

It is useful because it reproduces the upstream field shape that the Pico expects, including:
- wind lull, avg, gust,
- direction,
- pressure,
- temp,
- humidity,
- light,
- UV,
- solar radiation,
- rain,
- lightning,
- battery,
- report interval,
- nearcast fields.

---

## 4. SQLite schema overview

The local SQLite database has five main jobs:

1. store accepted weather readings,
2. queue those readings for AWS delivery,
3. store health/status history,
4. store a one-row-per-device latest snapshot,
5. store invalid, rejected, unknown, or duplicate ingest events for debugging.

Before any tables are created, `schema.sql` enables:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

That gives better service-style behavior and proper relational integrity.

---

## 5. Detailed SQLite schema reference

## 5.1 `weather_readings`

### Purpose
Stores one row per accepted weather observation.

### Schema

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

### Column explanations

| Column | Meaning |
|---|---|
| `id` | Internal SQLite surrogate key. Used by the delivery queue. |
| `source_node_id` | Meshtastic sender id, such as `!abcd1234`. |
| `source_name` | Optional node display name. |
| `msg_id` | Garden-side message id from the packet. Useful for tracing and cloud dedupe. |
| `source_ts_utc` | Source-origin timestamp from the payload. Required by `storage.py`. In practice this may be an epoch string from the Pico. |
| `received_at_utc` | Timestamp when the home Pi received and parsed the message. |
| `temp_c` | Air temperature in Celsius. |
| `humidity_pct` | Relative humidity percent. |
| `pressure_hpa` | Station pressure in hPa. |
| `wind_ms` | Wind speed in m/s. In the current parser this is average wind. |
| `wind_dir_deg` | Wind direction in degrees. |
| `rain_mm` | Rain for the represented interval in mm. |
| `raw_payload` | Full original JSON payload as text. Important for debugging and future reprocessing. |
| `payload_hash` | SHA-256 hash of the raw payload. |
| `created_at_utc` | Insert timestamp generated by SQLite. |

### Uniqueness rule
The table deduplicates on:
- `UNIQUE(source_node_id, source_ts_utc)`

That means local dedupe is based on **source node + source timestamp**, not source node + message id.

This is one of the most important implementation details in the MVP.

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_weather_received_at
ON weather_readings(received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_received
ON weather_readings(source_node_id, received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_msg
ON weather_readings(source_node_id, msg_id);
```

These support:
- recent history queries,
- per-device history queries,
- station + msg id troubleshooting.

---

## 5.2 `aws_delivery_queue`

### Purpose
Tracks whether each saved weather reading has been delivered to AWS.

### Schema

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

### Column explanations

| Column | Meaning |
|---|---|
| `id` | Internal queue row id, logged by the worker as `queue_id`. |
| `reading_id` | Foreign key to `weather_readings.id`. |
| `status` | Delivery state. In the current code, the meaningful values are `pending`, `retry`, and `delivered`. |
| `attempt_count` | Number of persisted delivery failures recorded so far. |
| `next_attempt_at_utc` | Earliest time the row should be retried. |
| `last_attempt_at_utc` | Timestamp of the most recent delivery attempt. |
| `delivered_at_utc` | Timestamp when AWS delivery succeeded. |
| `last_error` | Most recent failure text, truncated to 500 chars by `storage.py`. |
| `created_at_utc` | Queue row creation timestamp. |
| `updated_at_utc` | Queue row last update timestamp. |

### Constraints
- `reading_id` is unique, so one weather reading gets one queue row.
- `ON DELETE CASCADE` means deleting the weather row removes its queue row too.

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_delivery_status_next_attempt
ON aws_delivery_queue(status, next_attempt_at_utc, id);
```

This supports the worker’s polling query efficiently.

---

## 5.3 `device_health_events`

### Purpose
Stores every accepted health/status packet as historical telemetry.

### Schema

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

### Column explanations

| Column | Meaning |
|---|---|
| `id` | Internal row id. |
| `source_node_id` | Node that sent the health/status packet. |
| `source_name` | Optional friendly node name. |
| `msg_id` | Optional packet sequence/message id. |
| `source_ts_utc` | Optional originating timestamp from the payload. |
| `received_at_utc` | When the Pi received it. |
| `status` | Value from `sys`, such as `dbg`, `ok`, `warn`, or `error`. |
| `uptime_sec` | Device uptime if present. |
| `ip_address` | Current IP if present. |
| `error_reason` | Optional error text from `err`. |
| `raw_payload` | Full original JSON health payload. |
| `created_at_utc` | Insert time in SQLite. |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_health_source_received
ON device_health_events(source_node_id, received_at_utc);
```

This supports health history by device and recent event lookup.

---

## 5.4 `device_status_current`

### Purpose
Stores the latest known state snapshot for each source node.

This is a denormalized convenience table so code does not have to scan all history tables to answer “what is the latest known state of this device?”

### Schema

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

### Column explanations

| Column | Meaning |
|---|---|
| `source_node_id` | Primary key. One row per device/source. |
| `source_name` | Friendly node name if known. |
| `last_weather_at_utc` | Last accepted weather packet receive time. |
| `last_health_at_utc` | Last accepted health packet receive time. |
| `last_status` | Latest raw `sys` value. |
| `derived_state` | Reserved for higher-level state such as `healthy`, `offline`, etc. |
| `last_msg_id` | Most recent message id seen. |
| `last_source_ts_utc` | Most recent source-origin timestamp seen. |
| `last_uptime_sec` | Most recent uptime reported by health packet. |
| `last_ip_address` | Most recent IP reported by health packet. |
| `updated_at_utc` | Last update timestamp for this snapshot row. |

### Important implementation note
The table contains `derived_state`, but **current `storage.py` never sets it**. So the column exists, but the MVP is not yet deriving health/offline/degraded state locally.

---

## 5.5 `ingest_events`

### Purpose
Stores packets that were seen during ingest but were not stored as accepted weather rows or accepted health rows.

This is the diagnostics and audit table for bad or unusual traffic.

### Schema

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

### Column explanations

| Column | Meaning |
|---|---|
| `id` | Internal row id. |
| `received_at_utc` | When the Pi saw the packet. |
| `source_node_id` | Source device id if known. |
| `source_name` | Source device name if known. |
| `packet_type` | Parser classification such as `invalid`, `rejected`, `unknown`, or duplicate bookkeeping. |
| `reason` | Why it was classified that way. |
| `msg_id` | Message id if extractable. |
| `raw_payload` | Raw packet text. |
| `created_at_utc` | Insert time. |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_ingest_received
ON ingest_events(received_at_utc);
```

---

## 6. How the SQLite tables work together

### Successful weather flow

```text
valid weather packet received
  -> parser returns packet_type=weather
  -> insert into weather_readings
  -> insert pending row into aws_delivery_queue
  -> update device_status_current with latest weather timestamps/msg ids
```

### Duplicate weather flow

```text
duplicate weather packet received
  -> weather_readings insert hits UNIQUE(source_node_id, source_ts_utc)
  -> insert_weather returns "duplicate"
  -> listen_meshtastic.py records an ingest_events row
```

### Health flow

```text
valid health packet received
  -> parser returns packet_type=health
  -> insert into device_health_events
  -> update device_status_current with latest health values
```

### Invalid / rejected / unknown flow

```text
packet seen but not accepted
  -> parser returns invalid/rejected/unknown
  -> record_ingest_event()
  -> row saved in ingest_events
```

### Delivery success flow

```text
queue worker posts reading to AWS successfully
  -> mark_delivery_success(queue_id)
  -> status becomes delivered
  -> delivered_at_utc is set
```

### Delivery failure flow

```text
queue worker fails to post reading
  -> mark_delivery_failure(queue_id, error)
  -> attempt_count increments
  -> next_attempt_at_utc scheduled
  -> status becomes retry
```

---

## 7. Important SQLite and ingest semantics

## 7.1 Local dedupe key is timestamp-based
Locally, a weather reading is deduped by:
- `source_node_id`
- `source_ts_utc`

This is different from the design docs, which described deduping by message id.

## 7.2 Cloud dedupe key is message-id-based
In AWS, dedupe is based on:
- station/source node id
- `msg_id`

That means local and cloud dedupe are **not using the same natural key**.

### What that implies
- Two packets with the same `msg_id` but different `source_ts_utc` could both exist locally but be deduped by AWS.
- Two packets with the same `source_ts_utc` but different `msg_id` would collapse locally before AWS ever sees both.

That mismatch is not necessarily fatal for the MVP, but it is a real design inconsistency and worth fixing later.

## 7.3 Weather packets must have `source_ts_utc`
Because `insert_weather()` rejects missing timestamps, weather rows without `ts` from the source are not stored.

This is stricter than the parser alone. The parser will happily normalize weather with missing `ts`, but the storage layer will not accept it.

## 7.4 No local derived state yet
The design docs describe derived states like `healthy`, `degraded`, `offline`, and `bad_health`, but the current storage code does not calculate them. The schema leaves room for it, but the MVP does not implement it.

## 7.5 The raw payload is preserved everywhere important
This is a strong design choice in the current MVP:
- accepted weather rows keep `raw_payload`,
- accepted health rows keep `raw_payload`,
- rejected/unknown rows keep `raw_payload`.

That makes future debugging and schema evolution much easier.

---

## 8. AWS infrastructure

There are two CloudFormation templates:

- `weather-station-stack.yaml`: the authoring template
- `packaged-template.yaml`: the deployment-ready packaged template with S3 code references

### High-level AWS architecture

```text
HTTP API (API Gateway v2)
  -> POST /observations        -> ingest Lambda
  -> GET  /observations/latest -> read Lambda
  -> GET  /observations        -> read Lambda

Both Lambdas use one DynamoDB table.
```

---

## 9. CloudFormation resources in detail

## 9.1 Parameters

### `ProjectName`
Default: `weather-station`

Used in naming resources.

### `EnvironmentName`
Default: `dev`

Used to produce environment-specific names.

### `ApiSharedSecret`
- `NoEcho: true`
- minimum length 16

This is the shared secret expected in the `x-weatherstation-key` header.

### `TableName`
Default: `WeatherObservations`

Name of the DynamoDB table.

### `AllowedCorsOrigin`
Default: `*`

Used for API CORS headers.

---

## 9.2 DynamoDB table: `WeatherObservationsTable`

The table uses:
- `BillingMode: PAY_PER_REQUEST`
- partition key `pk` (string)
- sort key `sk` (string)

This is a **single-table design**.

Different logical record types live together in the same table and are distinguished by key patterns and attributes.

---

## 9.3 IAM role: `WeatherLambdaRole`

The Lambda role allows:

### DynamoDB actions
- `GetItem`
- `PutItem`
- `Query`
- `TransactWriteItems`

### CloudWatch Logs actions
- `CreateLogGroup`
- `CreateLogStream`
- `PutLogEvents`

That is enough for the current ingest and read Lambdas.

---

## 9.4 Lambda functions

### `IngestObservationFunction`
Runtime settings:
- Python 3.13
- handler `app.handler`
- 10 second timeout
- 256 MB memory
- arm64

Environment variables:
- `TABLE_NAME`
- `API_SHARED_SECRET`
- `ALLOWED_CORS_ORIGIN`

### `ReadObservationFunction`
Runtime settings:
- Python 3.13
- handler `app.handler`
- 10 second timeout
- 256 MB memory
- arm64

Environment variables:
- `TABLE_NAME`
- `ALLOWED_CORS_ORIGIN`

### Code packaging note
In `weather-station-stack.yaml`, the Lambda `Code` values are local directories:
- `ingest/`
- `read/`

In `packaged-template.yaml`, those are replaced with:
- `S3Bucket`
- `S3Key`

So `packaged-template.yaml` is the real deployable artifact after packaging.

---

## 9.5 API Gateway

The stack creates an API Gateway v2 HTTP API with:
- `POST /observations`
- `GET /observations/latest`
- `GET /observations`

A `$default` stage is created with `AutoDeploy: true`.

CORS allows:
- methods `GET`, `POST`, `OPTIONS`
- headers `content-type`, `x-weatherstation-key`

### Security note
The POST endpoint is protected inside the ingest Lambda by checking `x-weatherstation-key`.

The GET endpoints are not authenticated in the current Lambda code. That may be intentional for a simple website, but it is worth noting.

---

## 10. AWS ingest Lambda (`app.py`)

This Lambda handles `POST /observations`.

### Route handling
It only accepts:
- `POST /observations`

`OPTIONS` returns success for CORS.

Anything else returns 404.

### Authentication
It checks the request header:
- `x-weatherstation-key`

and compares it to `API_SHARED_SECRET`.

If the secret does not match, it returns:
- `401 unauthorized`

### Expected request body
It expects a body shaped like:

```json
{
  "payload": {
    "source_node_id": "!abcd1234",
    "source_name": "garden",
    "msg_id": 17,
    "source_ts_utc": "1773794743",
    "received_at_utc": "2026-03-18T00:45:46.982324Z",
    "weather": {
      "air_temp_c": 22.5,
      "relative_humidity_pct": 52,
      "station_pressure_hpa": 1011.4,
      "wind_avg_ms": 3.4,
      "wind_dir_deg": 230,
      "rain_interval_mm": 0.0
    }
  }
}
```

### Validation rules
Required top-level payload fields:
- `source_node_id` must be a string
- `msg_id` must be present and convertible to int
- `received_at_utc` must be ISO-8601 UTC with trailing `Z`

Optional:
- `source_name`
- `source_ts_utc`

`source_ts_utc` may be:
- epoch string
- ISO UTC string
- integer/decimal epoch

The weather object is validated field by field using `WEATHER_RULES`.

#### Supported validated weather fields
The Lambda accepts a wider schema than the current home SQLite schema stores. It can validate fields such as:
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

So the cloud-side contract is already more future-ready than the local home parser/schema.

### DynamoDB key design
The Lambda builds:
- `pk = "STATION#" + source_node_id`

and then three sort keys:
- observation row: `OBS#<received_at_utc>#<zero-padded msg_id>`
- latest row: `LATEST`
- dedupe row: `DEDUPE#<zero-padded msg_id>`

### Items written per accepted observation
The Lambda writes three logical items in one transaction.

#### 1. Dedupe item
Used to guarantee idempotency per station and message id.

Fields include:
- `pk`
- `sk = DEDUPE#...`
- `record_type = dedupe`
- `source_node_id`
- `msg_id`
- `received_at_utc`

This item is written with a condition that it must not already exist.

#### 2. Observation item
The historical time-series record.

Fields include:
- `pk`
- `sk = OBS#...`
- `record_type = observation`
- `source_node_id`
- `source_name`
- `msg_id`
- `source_ts_utc`
- `received_at_utc`
- `weather`
- `raw_payload`
- `ingested_at_utc`

#### 3. Latest item
The current/latest snapshot for the station.

Fields include:
- `pk`
- `sk = LATEST`
- `record_type = latest`
- `source_node_id`
- `source_name`
- `msg_id`
- `source_ts_utc`
- `received_at_utc`
- `weather`
- `latest_observation_sk`
- `updated_at_utc`

### Transaction behavior
All three items are written in one `TransactWriteItems` call.

That ensures:
- the historical observation,
- the dedupe marker,
- and the latest snapshot

stay consistent.

### Deduplication response behavior
If the transaction throws `TransactionCanceledException`, the Lambda returns:

```json
{
  "ok": true,
  "deduped": true,
  "source_node_id": "...",
  "msg_id": 17
}
```

This is intended to represent duplicate detection.

### Important implementation note
The code treats **any** `TransactionCanceledException` as dedupe. In practice that exception could theoretically happen for reasons other than the dedupe condition, though dedupe is the expected reason here.

---

## 11. AWS read Lambda (`appRead.py`)

This Lambda handles:
- `GET /observations/latest`
- `GET /observations`

### `GET /observations/latest`
Requires query string:
- `stationId`

It performs a `GetItem` with:
- `pk = STATION#<stationId>`
- `sk = LATEST`

If found, it returns the normalized item.

### `GET /observations`
Requires query string:
- `stationId`
- `from`
- `to`

Optional:
- `limit` default 200, minimum 1, maximum 1000

It queries:
- `pk = STATION#<stationId>`
- `sk BETWEEN OBS#<from> AND OBS#<to>~`

and sets:
- `ScanIndexForward = False`

So history is returned newest first.

### Why the `~` suffix matters
Because sort keys are strings, appending `~` on the upper bound ensures all keys beginning with `OBS#<to>` compare below the end key. It is a common lexicographic range-query trick.

---

## 12. End-to-end schema alignment

## 12.1 What lines up well
There are several strong alignments in the MVP:

- The home worker produces a payload shape that the AWS ingest Lambda accepts.
- The worker normalizes timestamps to `Z`, which the ingest Lambda requires.
- The local queue model and the cloud dedupe model together provide at-least-once delivery.
- Raw payloads are preserved both locally and in DynamoDB.

## 12.2 Where the current MVP is narrower than the intended design

### Local home schema is narrower than the Pico payload
The Pico emits rich weather fields, but the parser/storage/schema only preserve the basic weather core.

### Local dedupe key and cloud dedupe key differ
Local dedupe uses:
- `source_node_id + source_ts_utc`

Cloud dedupe uses:
- `source_node_id + msg_id`

### `derived_state` exists in schema but is not implemented
The design docs talk about health/offline/degraded logic, but the current code does not compute it.

### Read endpoints are open
The write API is protected by the shared secret, but the read API is not authenticated in the current Lambda implementation.

---

## 13. Practical interpretation of each database in the MVP

## 13.1 SQLite on the Raspberry Pi
This is the **operational durability layer**.

It answers:
- What weather did we successfully ingest locally?
- What health packets did we see?
- What weird/bad packets did we reject?
- What still needs to be sent to AWS?
- What delivery errors happened?

It is the place you would inspect first during debugging.

## 13.2 DynamoDB in AWS
This is the **cloud serving and dedupe layer**.

It answers:
- What is the latest reading for station X?
- What is the history for station X over a time window?
- Has message id Y for station X already been accepted?

It is optimized for:
- cheap serverless operation,
- fast latest reads,
- easy history queries,
- idempotent ingest.

---

## 14. Final summary

The current MVP is solidly structured around a durable local queue and a simple serverless cloud backend.

### Locally on the Pi
- `listen_meshtastic.py` ingests packets.
- `parser.py` classifies them.
- `storage.py` saves them to SQLite.
- `weather_readings` stores accepted weather.
- `aws_delivery_queue` persists delivery state.
- `device_health_events` stores health history.
- `device_status_current` stores latest known per-device values.
- `ingest_events` stores rejected/unknown/duplicate traffic.
- `queue_worker.py` drains the queue to AWS.

### In AWS
- API Gateway exposes three routes.
- The ingest Lambda validates and writes observations.
- DynamoDB stores observation history, latest snapshots, and dedupe items.
- The read Lambda serves latest and history queries.

### The most important implementation realities right now
- local weather storage requires `source_ts_utc`,
- local dedupe is based on `source_ts_utc`,
- cloud dedupe is based on `msg_id`,
- the local schema currently stores only the core weather fields,
- `derived_state` is planned in schema but not yet implemented.

Those are the key facts to understand if you are going to extend this MVP into the next version.
