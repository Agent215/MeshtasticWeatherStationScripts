# Weather Station MVP Architecture and Data Model

## Overview

This document explains how the Weather Station MVP works end to end, with a focus on:

- the **SQLite database** on the home server
- the **home server Python services**
- the **AWS CloudFormation stack**
- the **DynamoDB data model** used by the cloud API

The current system is a store-and-forward pipeline:

1. A garden-side controller produces compact weather JSON and sends it over Meshtastic.
2. The home Raspberry Pi listens to Meshtastic packets, parses them, and stores accepted data in SQLite.
3. A queue worker posts pending weather readings from SQLite to AWS.
4. AWS writes historical observations and a latest snapshot into DynamoDB.
5. Read APIs serve latest and historical data from DynamoDB.

The design goal is reliability first:

- local capture should continue even if AWS is temporarily unavailable
- cloud writes should be idempotent
- the home server should keep enough metadata to debug packet flow and delivery issues

---

# 1. End-to-end flow

## 1.1 Data path

```text
Tempest / upstream weather source
  -> garden-side controller (`gardenNode/main.py`)
  -> Meshtastic text message
  -> home Meshtastic node over USB serial
  -> `listen_meshtastic.py`
  -> `parser.py`
  -> `storage.py`
  -> SQLite

SQLite weather backlog
  -> `queue_worker.py`
  -> AWS API Gateway HTTP API
  -> ingest Lambda
  -> DynamoDB

Read clients
  -> API Gateway
  -> read Lambda
  -> DynamoDB
```

## 1.2 Reliability model

The home server is the durable ingest point. The important sequence is:

1. Receive packet from Meshtastic.
2. Parse and validate it.
3. Save it to SQLite.
4. Enqueue it for cloud delivery.
5. Retry cloud delivery until it succeeds.

That means a temporary internet outage does not prevent local capture. The queue worker can catch up later.

## 1.3 Identity model for weather observations

The system currently treats a weather observation as uniquely identified by:

```text
(source_node_id, source_ts_utc)
```

That identity is used in both places:

- **SQLite**: `weather_readings` has `UNIQUE(source_node_id, source_ts_utc)`
- **DynamoDB**: observation items are written with sort keys derived from `source_ts_utc`

`msg_id` still matters, but it is now mainly transport/debug metadata rather than the canonical dedupe key for weather history.

---

# 2. Codebase map

This document describes the current production architecture. Not every production home-server module described below is currently checked into this repository.

## 2.1 Home-side files

### `listen_meshtastic.py`
Long-running listener process on the Raspberry Pi. It connects to the home Meshtastic node over USB serial, receives packets, and routes them into storage.

In this repository, the checked-in Meshtastic-side utility is `home_server_listen_meshtastic.py`. The fuller production listener/storage pipeline described here is not fully present in the repo yet.

### `parser.py`
Parses the decoded text payload from Meshtastic and classifies it as:

- weather
- health
- invalid
- rejected
- unknown

### `storage.py`
Implements all SQLite writes and queue state transitions.

### `db.py`
Opens SQLite connections and applies database pragmas.

### `schema.sql`
Defines the SQLite schema.

### `queue_worker.py`
Polls pending rows from the local delivery queue and POSTs them to AWS.

## 2.2 Garden-side producer files

### `gardenNode/main.py`
Garden-side controller / bridge. It rate-limits outgoing weather messages, attaches `msg_id`, carries forward the source timestamp, and sends health/debug packets periodically.

### `mocks/mock_tempest_udp_sender.py`
A local test generator for Tempest-style UDP packets.

## 2.3 AWS files

### `aws/weather-station-stack.yaml`
Source CloudFormation template.

### `aws/packaged-template.yaml`
Packaged CloudFormation template with Lambda code artifacts uploaded to S3.

### `aws/ingest/app.py`
AWS ingest Lambda.

### `aws/read/app.py`
AWS read Lambda.

---

# 3. Home server runtime behavior

## 3.1 `listen_meshtastic.py`

This is the entry point for inbound Meshtastic traffic on the home server.

### What it does

- opens a Meshtastic serial interface
- subscribes to pubsub topics for packet receive and connection state
- extracts sender metadata from packets
- decodes the text payload
- passes the text into the parser
- calls storage functions based on parser output
- logs structured JSON events
- reconnects automatically if the serial link drops

### Packet metadata captured

The listener extracts:

- `fromId` -> stored as `source_node_id`
- `user.longName` or `user.shortName` -> stored as `source_name` when available

This is the only place `source_name` is discovered. The cloud does not derive it on its own. If Meshtastic packet metadata does not include a name, `source_name` remains null through the rest of the pipeline.

### How packets are handled

The listener only treats `TEXT_MESSAGE_APP` packets as application payloads to parse. Other packet classes can still be logged, but weather and health ingestion happens through text packets.

### Weather path

For a parsed weather event, the listener calls:

```python
result = insert_weather(parsed)
```

If `insert_weather` returns `"duplicate"`, the listener records an ingest event instead of inserting a second weather row.

### Health path

For a parsed health packet, the listener calls:

```python
insert_health(parsed)
```

### Invalid / rejected / unknown path

Anything not accepted as weather or health is stored in `ingest_events` through:

```python
record_ingest_event(parsed)
```

That makes malformed or unexpected packets visible for troubleshooting.

## 3.2 `parser.py`

The parser converts raw JSON text into a structured `ParsedEvent`.

### `ParsedEvent` fields

| Field | Meaning |
|---|---|
| `packet_type` | Parser classification such as `weather`, `health`, `invalid`, `rejected`, or `unknown` |
| `reason` | Optional explanation when a packet is rejected or not recognized |
| `source_node_id` | Meshtastic sender id |
| `source_name` | Sender name if available |
| `msg_id` | Message id extracted from the payload if available |
| `received_at_utc` | When the home server received the packet |
| `source_ts_utc` | Timestamp reported by the originating source payload |
| `raw_payload` | Original text received from Meshtastic |
| `normalized` | Parsed structured fields for weather or health packets |

### Weather payload contract

The checked-in garden bridge currently emits weather observations as compact JSON with `et="obs_st"` plus the compact weather fields described below.

The production parser described here recognizes weather when the JSON object contains all of these keys:

- `i` = message id
- `t` = temperature
- `h` = humidity
- `p` = pressure
- `w` = wind
- `d` = wind direction
- `r` = rain

Optional:

- `ts` = source timestamp
- `et` = event type marker from the current garden bridge
- additional optional weather/detail fields such as `g`, `l`, `uv`, `sr`, `lux`, `bat`, `ld`, `lc`, `pt`, `ri`, `rd`, `nr`, `nrd`, and `pa`

The parser normalizes that compact payload into:

- `msg_id`
- `temp_c`
- `humidity_pct`
- `pressure_hpa`
- `wind_ms`
- `wind_dir_deg`
- `rain_mm`
- `source_ts_utc`

### Weather validation ranges

The parser enforces these ranges before local storage:

- temperature: `-60` to `70`
- humidity: `0` to `100`
- pressure: `800` to `1200`
- wind speed: `0` to `100`
- wind direction: `0` to `360`
- rain: `0` to `500`

A payload with the right weather shape but invalid values becomes `packet_type="rejected"`.

### Health payload contract

The current bridge heartbeat uses `sys = "dbg"` and includes uptime, IP, and diagnostic counters.

The parser recognizes health packets when the JSON object contains `sys`.

It normalizes fields such as:

- `status` from `sys`
- `msg_id` from `i`
- `uptime_sec` from `up`
- `ip_address` from `ip`
- `error_reason` from `err`
- `source_ts_utc` from `ts`

### Invalid and unknown classifications

- `invalid` means the payload could not be parsed or was structurally broken
- `unknown` means the payload was valid JSON but did not match known weather or health schemas
- the current garden bridge also emits `evt_precip`, `evt_strike`, `device_status`, and `hub_status` payloads; those payloads are part of the transport contract even though the fuller production parser/storage implementation is not checked into this repository

## 3.3 `storage.py`

`storage.py` is the concrete storage layer for the home server. It owns all SQLite writes and queue state transitions.

### Key helper functions

#### `utc_now()`
Returns the current UTC timestamp as an ISO string with timezone offset.

#### `compute_next_attempt(attempt_count)`
Computes the next retry time for a failed AWS delivery.

The retry schedule is:

- attempt 1 failure -> retry in 30 seconds
- attempt 2 failure -> retry in 120 seconds
- attempt 3 failure -> retry in 600 seconds
- attempt 4+ failure -> retry in 1800 seconds

This is the persistent queue backoff schedule stored in SQLite.

### `record_ingest_event(event, packet_type_override=None, reason_override=None)`

Writes a row into `ingest_events`.

This is used for:

- invalid payloads
- unknown payloads
- rejected payloads
- duplicate weather observations that were recognized but not inserted again

The function preserves the packet metadata and the original payload for analysis.

### `insert_weather(event)`

This is the main SQLite write path for accepted weather observations.

#### What it requires

- `event.normalized` must exist
- `normalized["source_ts_utc"]` must be present and non-empty

If `source_ts_utc` is missing, `insert_weather` raises:

```text
weather_missing_source_ts_utc
```

That requirement is important because the local database uses `source_ts_utc` as part of the uniqueness rule for weather rows.

#### What it writes

On success, `insert_weather` performs three database actions in one SQLite transaction context:

1. insert a row into `weather_readings`
2. insert a row into `aws_delivery_queue`
3. upsert the latest device snapshot into `device_status_current`

It also computes and stores a SHA-256 hash of the original raw payload using `payload_hash()` from `parser.py`.

#### Duplicate handling

If the weather insert violates the unique constraint on `(source_node_id, source_ts_utc)`, SQLite raises `sqlite3.IntegrityError`. `insert_weather` catches that and returns:

```text
duplicate
```

The caller can then record the duplicate in `ingest_events` without creating a second weather row or a second queue row.

This gives the home server a clear separation between:

- canonical stored weather history
- duplicate packet visibility

#### Device status update on weather insert

`insert_weather` updates `device_status_current` with:

- `source_name`
- `last_weather_at_utc`
- `last_msg_id`
- `last_source_ts_utc`
- `updated_at_utc`

The upsert uses `ON CONFLICT(source_node_id) DO UPDATE`, so there is always a single current row per source node.

### `insert_health(event)`

This inserts a row into `device_health_events` and updates the current device snapshot.

#### What it writes into `device_health_events`

- source metadata
- optional message id
- optional source timestamp
- receive time
- status string
- uptime
- IP address
- error reason
- raw payload

#### What it updates in `device_status_current`

- `source_name`
- `last_health_at_utc`
- `last_status`
- `last_msg_id`
- `last_source_ts_utc`
- `last_uptime_sec`
- `last_ip_address`
- `updated_at_utc`

Health packets therefore feed the operational state view of each device even though they are not pushed to AWS history.

### `fetch_pending_deliveries(limit=10)`

This is the queue worker’s read path into SQLite.

It joins `aws_delivery_queue` to `weather_readings` and returns rows that are eligible for delivery.

#### Delivery eligibility rule

A queue row is returned when:

- `status` is `pending` or `retry`
- and `next_attempt_at_utc` is null or due

The query checks both:

- `next_attempt_at_utc <= CURRENT_TIMESTAMP`
- `next_attempt_at_utc <= utc_now()`

This makes the query tolerant of SQLite time formatting differences between `CURRENT_TIMESTAMP` and Python-generated ISO strings.

#### Fields returned to the worker

The joined row contains:

- queue metadata: `queue_id`, `reading_id`, `status`, `attempt_count`, `next_attempt_at_utc`
- identity fields: `source_node_id`, `source_name`, `msg_id`, `source_ts_utc`, `received_at_utc`
- weather payload columns
- `raw_payload`

Rows are ordered by `q.id ASC`, which gives a stable FIFO-like processing order.

### `mark_delivery_success(queue_id)`

Marks a queue row as delivered.

It updates:

- `status = 'delivered'`
- `delivered_at_utc = CURRENT_TIMESTAMP`
- `last_attempt_at_utc = CURRENT_TIMESTAMP`
- `updated_at_utc = CURRENT_TIMESTAMP`
- `last_error = NULL`

This means successful delivery permanently clears the retry state.

### `mark_delivery_failure(queue_id, error)`

Marks a queue row as failed and schedules the next retry.

It:

1. reads the current `attempt_count`
2. increments it
3. computes the next retry time with `compute_next_attempt`
4. updates the queue row

Updated fields are:

- `status = 'retry'`
- `attempt_count`
- `next_attempt_at_utc`
- `last_attempt_at_utc`
- `updated_at_utc`
- `last_error`

`last_error` is truncated to 500 characters before storage.

This is the persistent retry state for the cloud delivery worker.

## 3.4 `queue_worker.py`

The queue worker is the home server process that drains SQLite into AWS.

### Core responsibilities

- load API URL and secret from `.env`
- poll SQLite for pending deliveries
- build the AWS request body
- POST to the cloud API
- retry transient HTTP/network errors immediately with exponential backoff
- persist final success or failure state in SQLite
- integrate with systemd notify/watchdog

### Environment variables

The worker expects:

- `API_URL`
- `API_KEY`

The `.env` file is read from:

```text
<project root>/.env
```

### Request body construction

For each pending weather row, `build_api_request_body()` sends a JSON body shaped like:

```json
{
  "payload": {
    "source_node_id": "...",
    "source_name": "...",
    "msg_id": 17,
    "source_ts_utc": "...",
    "received_at_utc": "...",
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

The worker sends `API_KEY` in the `x-weatherstation-key` header. Within `weather`, the cloud schema uses expanded field names such as `air_temp_c`, `relative_humidity_pct`, `station_pressure_hpa`, `wind_avg_ms`, `wind_gust_ms`, `wind_lull_ms`, `wind_dir_deg`, and `rain_interval_mm`.

### `msg_id` versus `source_ts_utc`

The queue worker still sends both values:

- `msg_id` remains useful for tracing and debugging
- `source_ts_utc` is the important identity field for cloud history dedupe

### Immediate in-process retry behavior

In addition to the persistent SQLite queue retry schedule, the worker also performs immediate retries inside a single processing attempt.

The in-process retry logic uses:

- initial delay: 2 seconds
- exponential backoff
- capped at 30 seconds
- max HTTP post attempts per processing cycle: 5

Retryable errors include:

- HTTP `429`
- HTTP `500`
- HTTP `502`
- HTTP `503`
- HTTP `504`
- URL/network errors
- timeouts

Non-retryable errors are treated as final failures for that queue cycle and are written back to SQLite.

### Success path

When AWS accepts the observation, the worker:

1. calls `mark_delivery_success(queue_id)`
2. logs the success
3. records whether the cloud considered the write deduped

### Failure path

When posting fails, the worker:

1. calls `mark_delivery_failure(queue_id, error)`
2. stores the error and next retry time in SQLite
3. moves on to the next polling cycle

## 3.5 `db.py`

`db.py` defines the SQLite database location and connection settings.

### Database path

The current database file is:

```text
~/weatherstation-home/weatherstation/weatherstation.db
```

### Connection behavior

Every connection:

- uses `sqlite3.Row` row objects
- enables `PRAGMA foreign_keys=ON`
- enables `PRAGMA journal_mode=WAL`

---

# 4. SQLite database design

The home server uses SQLite as its operational database.

## 4.1 Why SQLite is a good fit here

SQLite is appropriate for this MVP because it is:

- local and durable
- simple to inspect manually
- transactional
- reliable enough for small service workloads
- easy to back up
- efficient for a single-device store-and-forward pipeline

## 4.2 Database-wide settings

Before tables are used, the schema enables:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
```

### `journal_mode=WAL`
Write-ahead logging improves concurrency and crash resilience for a long-running service workload.

### `foreign_keys=ON`
SQLite enforces relationships such as the delivery queue row referencing a real weather reading.

---

# 5. SQLite schema reference

## 5.1 `weather_readings`

### Purpose

This is the canonical local history of accepted weather observations.

Each row represents one unique weather observation from one source node.

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
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal surrogate key for SQLite. Other tables, especially `aws_delivery_queue`, reference this value. |
| `source_node_id` | `TEXT NOT NULL` | Meshtastic node id of the sender, such as `!5c32d4d9`. This is the station identity on the home side. |
| `source_name` | `TEXT` | Human-readable node name from Meshtastic packet metadata when available. |
| `msg_id` | `INTEGER NOT NULL` | Message id assigned on the sender side. Useful for debugging, ordering, and transport tracing. Not the canonical dedupe key for weather rows. |
| `source_ts_utc` | `TEXT NOT NULL` | Timestamp from the source payload. This is the natural identity timestamp for a weather observation in the current design. |
| `received_at_utc` | `TEXT NOT NULL` | When the home server received and processed the packet. |
| `temp_c` | `REAL NOT NULL` | Temperature in Celsius. |
| `humidity_pct` | `REAL NOT NULL` | Relative humidity percentage. |
| `pressure_hpa` | `REAL NOT NULL` | Pressure in hectopascals. |
| `wind_ms` | `REAL NOT NULL` | Wind speed in meters per second. In the current compact payload this is average wind speed. |
| `wind_dir_deg` | `INTEGER NOT NULL` | Wind direction in degrees. |
| `rain_mm` | `REAL NOT NULL` | Rain amount in millimeters for the reporting interval represented by the packet. |
| `raw_payload` | `TEXT NOT NULL` | Original payload string exactly as received through Meshtastic. |
| `payload_hash` | `TEXT` | SHA-256 hash of `raw_payload`. Useful for diagnostics and for verifying that two packets had identical bodies. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When the row was inserted into SQLite. |

### Uniqueness rule

```sql
UNIQUE(source_node_id, source_ts_utc)
```

This is the home-side weather dedupe rule. It means one source node can only have one stored weather reading for a given source timestamp.

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_weather_received_at
ON weather_readings(received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_received
ON weather_readings(source_node_id, received_at_utc);

CREATE INDEX IF NOT EXISTS idx_weather_source_msg
ON weather_readings(source_node_id, msg_id);
```

### What the indexes support

- `idx_weather_received_at`: recent ingest inspection by local receive time
- `idx_weather_source_received`: per-station history lookups
- `idx_weather_source_msg`: packet tracing by source and sender-side message id

## 5.2 `aws_delivery_queue`

### Purpose

Tracks whether each local weather observation has been delivered to AWS.

Each weather row can have exactly one queue row.

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
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal queue row id. This is logged by the worker as `queue_id`. |
| `reading_id` | `INTEGER NOT NULL` | Foreign key to `weather_readings.id`. |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | Delivery state. In the current implementation the active values are `pending`, `retry`, and `delivered`. |
| `attempt_count` | `INTEGER NOT NULL DEFAULT 0` | Number of failed queue delivery cycles recorded so far. |
| `next_attempt_at_utc` | `TEXT` | Earliest UTC time the worker should retry the row. |
| `last_attempt_at_utc` | `TEXT` | Time of the last attempt to deliver this row to AWS. |
| `delivered_at_utc` | `TEXT` | Time the row was successfully accepted by AWS. |
| `last_error` | `TEXT` | Last error message seen during delivery. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When the queue row was created. |
| `updated_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When the queue row was last modified. |

### Constraints

- `FOREIGN KEY(reading_id) ... ON DELETE CASCADE` keeps queue rows tied to real weather rows.
- `UNIQUE(reading_id)` guarantees one queue row per weather reading.

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_delivery_status_next_attempt
ON aws_delivery_queue(status, next_attempt_at_utc, id);
```

This supports the worker query pattern of “find the next due pending/retry rows.”

## 5.3 `device_health_events`

### Purpose

Stores inbound health/debug packets from the remote device.

This is operational telemetry rather than cloud-served weather history.

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
| `msg_id` | `INTEGER` | Optional message id from the health payload. |
| `source_ts_utc` | `TEXT` | Optional source timestamp from the health payload. |
| `received_at_utc` | `TEXT NOT NULL` | When the home server received the health packet. |
| `status` | `TEXT NOT NULL` | Health status string from the payload, for example `dbg`. |
| `uptime_sec` | `INTEGER` | Uptime in seconds if supplied. |
| `ip_address` | `TEXT` | Sender IP address if supplied by the health payload. |
| `error_reason` | `TEXT` | Optional error detail from the health payload. |
| `raw_payload` | `TEXT NOT NULL` | Original raw health/debug payload. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When the row was inserted into SQLite. |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_health_source_received
ON device_health_events(source_node_id, received_at_utc);
```

This supports health history queries by device and time.

## 5.4 `device_status_current`

### Purpose

Stores the latest known per-device snapshot.

This table avoids scanning all history every time the system wants a current status view.

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
| `source_node_id` | `TEXT PRIMARY KEY` | One row per source node. |
| `source_name` | `TEXT` | Latest known human-readable node name. |
| `last_weather_at_utc` | `TEXT` | Home-server receive time of the most recent accepted weather packet from this device. |
| `last_health_at_utc` | `TEXT` | Home-server receive time of the most recent health packet from this device. |
| `last_status` | `TEXT` | Most recent health status string, such as `dbg`. |
| `derived_state` | `TEXT` | Reserved field for higher-level state derivation. The current `storage.py` does not actively populate it. |
| `last_msg_id` | `INTEGER` | Most recent message id seen for this device, whether from a weather or health update path. |
| `last_source_ts_utc` | `TEXT` | Most recent source-side timestamp seen from this device. |
| `last_uptime_sec` | `INTEGER` | Last reported uptime from a health packet. |
| `last_ip_address` | `TEXT` | Last reported IP address from a health packet. |
| `updated_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | When this snapshot row was last refreshed. |

### How it is maintained

- weather inserts update the weather-related columns
- health inserts update the health-related columns
- the row is upserted with `ON CONFLICT(source_node_id)`

## 5.5 `ingest_events`

### Purpose

Stores packets that were seen but not accepted into primary weather history.

This is the diagnostic ledger for packet ingestion.

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
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Internal event id. |
| `received_at_utc` | `TEXT NOT NULL` | When the packet was seen by the home server. |
| `source_node_id` | `TEXT` | Sender id if known. |
| `source_name` | `TEXT` | Sender name if known. |
| `packet_type` | `TEXT NOT NULL` | Parser category such as `invalid`, `unknown`, `rejected`, or duplicate bookkeeping. |
| `reason` | `TEXT` | Human-readable explanation of why the packet was not accepted into weather history. |
| `msg_id` | `INTEGER` | Message id if it could be extracted. |
| `raw_payload` | `TEXT` | Original raw payload for debugging and replay analysis. |
| `created_at_utc` | `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` | Insert time. |

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_ingest_received
ON ingest_events(received_at_utc);
```

This supports recent ingest troubleshooting.

---

# 6. How the SQLite tables work together

## 6.1 Accepted weather observation

```text
Meshtastic text packet
  -> parser classifies as weather
  -> storage inserts into weather_readings
  -> storage inserts one aws_delivery_queue row
  -> storage updates device_status_current
```

## 6.2 Duplicate weather observation

```text
Meshtastic text packet
  -> parser classifies as weather
  -> weather_readings insert hits UNIQUE(source_node_id, source_ts_utc)
  -> insert_weather returns "duplicate"
  -> listener records the packet in ingest_events
```

Result: the duplicate is visible, but there is still only one canonical weather row and one queue row.

## 6.3 Health/debug packet

```text
Meshtastic text packet
  -> parser classifies as health
  -> storage inserts device_health_events row
  -> storage updates device_status_current
```

## 6.4 Invalid or unknown packet

```text
Meshtastic text packet
  -> parser returns invalid / rejected / unknown
  -> storage records one ingest_events row
```

## 6.5 Successful AWS delivery

```text
queue_worker fetches pending row
  -> builds request body
  -> POST /observations succeeds
  -> mark_delivery_success(queue_id)
  -> row becomes delivered
```

## 6.6 Failed AWS delivery

```text
queue_worker fetches pending row
  -> POST /observations fails
  -> mark_delivery_failure(queue_id, error)
  -> row becomes retry
  -> next_attempt_at_utc is scheduled
```

---

# 7. Garden-side sender behavior relevant to the home/cloud pipeline

Although the home server is the main focus of this document, several garden-side behaviors are important for understanding the data model.

## 7.1 `gardenNode/main.py` forwarded payloads and pacing

The checked-in garden bridge supports these forwarded payload types:

- `obs_st`
- `evt_precip`
- `evt_strike`
- `device_status`
- `hub_status`

The controller applies these per-type forward intervals:

- `obs_st`: 60 seconds
- `evt_precip`: 60 seconds
- `evt_strike`: 30 seconds
- `device_status`: 60 seconds
- `hub_status`: 60 seconds

It also enforces a global minimum 5-second gap between UART sends and uses a small priority queue that replaces the latest queued `obs_st`, `device_status`, and `hub_status` snapshot instead of stacking stale copies.

This explains why the sender’s own debug counter or upstream packet cadence can drift from cloud observation counts.

## 7.2 `msg_id` is generated on the sender side

The controller increments a local `msg_id` for each forwarded message. That means:

- it is useful for tracing transport behavior
- it can reset after reboot
- it is not ideal as the canonical cloud dedupe key for weather history

## 7.3 Health/debug heartbeat

The controller also sends health/debug packets periodically with:

```text
sys = "dbg"
```

and a 15-minute heartbeat interval.

These packets use the same general transport path but are stored locally in `device_health_events` rather than being pushed as weather history to AWS.

---

# 8. AWS architecture

## 8.1 High-level cloud design

The cloud side consists of:

- API Gateway HTTP API
- ingest Lambda for `POST /observations`
- read Lambda for `GET /observations` and `GET /observations/latest`
- one DynamoDB table using a single-table key design

## 8.2 CloudFormation template

`weather-station-stack.yaml` defines the stack.

### Parameters

| Parameter | Purpose |
|---|---|
| `ProjectName` | Prefix used in resource names |
| `EnvironmentName` | Environment discriminator such as `dev` |
| `ApiSharedSecret` | Shared secret used by the ingest API |
| `TableName` | DynamoDB table name |
| `AllowedCorsOrigin` | CORS origin returned by the API |

## 8.3 Main AWS resources

### DynamoDB table
A single table with:

- partition key: `pk`
- sort key: `sk`

Billing mode is pay-per-request.

### IAM role
Allows the Lambdas to:

- read and write DynamoDB
- query DynamoDB
- write CloudWatch Logs

### Ingest Lambda
Handles authenticated weather writes.

### Read Lambda
Serves latest and historical observation queries.

### HTTP API routes

- `POST /observations`
- `GET /observations/latest`
- `GET /observations`

---

# 9. DynamoDB data model

DynamoDB is not a fixed-column relational database. Instead, this system uses item types with shared keys and type-specific attributes.

## 9.1 Partition key

All items for a station share:

```text
pk = STATION#<source_node_id>
```

Example:

```text
STATION#!5c32d4d9
```

This groups a station’s history and latest snapshot under the same partition.

## 9.2 Sort key patterns

The current logical item types are:

### Observation item
```text
sk = OBS#<normalized_source_ts_utc>#WEATHER
```

### Latest item
```text
sk = LATEST
```

There is no longer a separate `DEDUPE#...` item in the current design. Cloud dedupe happens by conditionally writing the observation item itself.

## 9.3 Why the observation sort key uses `source_ts_utc`

Using the source timestamp gives the cloud a stable event identity that survives retries and sender restarts better than a sender-local message counter.

Because timestamps are normalized to sortable UTC ISO strings, lexicographic ordering also matches time ordering.

---

# 10. DynamoDB item types in detail

## 10.1 Observation item

### Key pattern

- `pk = STATION#<source_node_id>`
- `sk = OBS#<normalized_source_ts_utc>#WEATHER`

### Attributes

| Attribute | Meaning |
|---|---|
| `pk` | Station partition key |
| `sk` | Observation sort key |
| `record_type` | Literal `observation` |
| `observation_type` | Literal `WEATHER` |
| `source_node_id` | Station id |
| `source_name` | Optional node name passed through from Meshtastic metadata |
| `msg_id` | Sender-side message id for tracing |
| `source_ts_utc` | Normalized source timestamp used as the canonical identity timestamp |
| `source_ts_raw` | Original source timestamp value as sent by the home server |
| `source_ts_sort_utc` | Normalized sortable UTC timestamp |
| `received_at_utc` | Normalized home-server receive timestamp |
| `weather` | Nested weather object |
| `raw_payload` | Full request body received by the ingest Lambda |
| `ingested_at_utc` | Time the observation was written to DynamoDB |

### Role

This is the durable cloud history record.

## 10.2 Latest item

### Key pattern

- `pk = STATION#<source_node_id>`
- `sk = LATEST`

### Attributes

| Attribute | Meaning |
|---|---|
| `pk` | Station partition key |
| `sk` | Literal `LATEST` |
| `record_type` | Literal `latest` |
| `observation_type` | Literal `WEATHER` |
| `source_node_id` | Station id |
| `source_name` | Optional latest node name |
| `msg_id` | Sender-side message id associated with the latest reading |
| `source_ts_utc` | Normalized source timestamp of the latest reading |
| `source_ts_raw` | Original source timestamp value from the request |
| `source_ts_sort_utc` | Normalized sortable source timestamp |
| `received_at_utc` | Normalized home-server receive timestamp |
| `weather` | Nested weather object of the newest reading |
| `latest_observation_sk` | Sort key of the observation item that this latest snapshot points to |
| `updated_at_utc` | Time the latest snapshot was refreshed |

### Role

This makes latest-read operations a simple point lookup rather than a time-range query.

---

# 11. AWS ingest Lambda

## 11.1 Route and authentication

The ingest Lambda only accepts:

- method: `POST`
- path: `/observations`

Requests must provide:

```http
x-weatherstation-key: <shared secret>
```

The header value must match `API_SHARED_SECRET`.

## 11.2 Request body

The ingest Lambda expects a JSON body shaped like:

```json
{
  "payload": {
    "source_node_id": "!5c32d4d9",
    "source_name": "optional",
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

## 11.3 Validation behavior

The Lambda validates:

- `payload.source_node_id` must be a string
- `payload.msg_id` must be integer-convertible
- `payload.received_at_utc` must be a valid timestamp
- `payload.source_ts_utc` must be present for weather identity
- `payload.weather` must be an object

It also validates recognized weather fields against numeric range rules.

The cloud schema is richer than the local parser. The ingest Lambda can validate additional optional weather fields such as:

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

## 11.4 Timestamp normalization

The ingest Lambda normalizes timestamps into a fixed sortable UTC string format.

It accepts either:

- ISO-8601 UTC strings
- epoch values as numbers
- epoch values as strings

It also normalizes milliseconds and microseconds when needed.

This normalized form is what becomes:

- the observation identity timestamp
- the observation sort key component
- the `source_ts_sort_utc` attribute

## 11.5 Cloud dedupe behavior

The current cloud dedupe strategy is:

```text
one station + one normalized source timestamp + WEATHER record type = one observation item
```

The Lambda writes the observation item with a conditional expression:

```text
attribute_not_exists(pk) AND attribute_not_exists(sk)
```

If that conditional write fails, the Lambda returns a successful response with `deduped: true`.

This means the observation item itself is the dedupe gate.

There is no separate dedupe marker row anymore.

## 11.6 Latest item update rule

After inserting the observation item, the Lambda writes the `LATEST` item only when the incoming observation is at least as new as the current latest snapshot.

That prevents an out-of-order older observation from moving the latest pointer backward.

---

# 12. AWS read Lambda

## 12.1 Supported routes

### `GET /observations/latest`

Required query parameter:

- `stationId`

This performs a point lookup on:

```text
pk = STATION#<stationId>
sk = LATEST
```

### `GET /observations`

Required query parameters:

- `stationId`
- `from`
- `to`

Optional:

- `limit` with default `200` and maximum `1000`
- `order=asc` or `order=desc`, with `desc` as the default
- `nextToken` for raw pagination
- `sample` with minimum `1` and maximum `2000`

## 12.2 Time-range query behavior

The read Lambda does not normalize `from` and `to`. Callers must send timestamps that already match the sortable UTC form used in the stored sort keys, for example `2026-03-18T00:45:46.982324Z`.

It then queries the partition using a sort key range like:

```text
from: OBS#<from>
to:   OBS#<to>~
```

The trailing `~` is a lexical upper-bound trick that ensures all matching observation keys in the time range are included.

In raw mode, the query uses `ScanIndexForward = False` by default and flips to ascending order only when `order=asc`.

When `sample` is provided, the Lambda scans the full matching window in ascending order, projects only the fields needed for sampling, caps the scan at 50,000 matched items, and returns an evenly sampled subset instead of paginated raw results.

## 12.3 Response structure

### Latest response

```json
{
  "ok": true,
  "item": { ... }
}
```

### Raw history response

```json
{
  "ok": true,
  "mode": "raw",
  "stationId": "!5c32d4d9",
  "from": "2026-03-18T00:00:00.000000Z",
  "to": "2026-03-18T23:59:59.999999Z",
  "order": "desc",
  "count": 123,
  "items": [ ... ],
  "nextToken": "..."
}
```

`nextToken` is present only when more raw results are available.

### Sampled history response

```json
{
  "ok": true,
  "mode": "sampled",
  "stationId": "!5c32d4d9",
  "from": "2026-03-18T00:00:00.000000Z",
  "to": "2026-03-18T23:59:59.999999Z",
  "sampleRequested": 200,
  "totalMatched": 1234,
  "count": 200,
  "items": [ ... ]
}
```

---

# 13. How the two databases relate to each other

## 13.1 SQLite is the operational database

SQLite is responsible for:

- capturing inbound packets durably
- deduping local weather observations
- tracking delivery attempts to AWS
- storing health/debug history
- recording parse failures and unexpected payloads

It is the home server’s authoritative event journal and backlog.

## 13.2 DynamoDB is the cloud serving database

DynamoDB is responsible for:

- storing weather history for external reads
- serving the latest observation quickly
- absorbing repeated uploads idempotently

It is the cloud publication layer rather than the primary operational ingest store.

## 13.3 Why this split works well

This division gives the system good failure behavior:

- if AWS is down, local capture still works
- if the network is flaky, the queue can retry later
- if the same observation is retried multiple times, cloud history stays stable
- if an observation arrives out of order, history is still preserved without corrupting the latest pointer

---

# 14. Key operational details

## 14.1 `source_name` behavior

`source_name` is not set in AWS independently. It is only passed through from Meshtastic packet metadata on the home server.

The discovery path is:

```text
packet.user.longName or packet.user.shortName
  -> parser ParsedEvent.source_name
  -> SQLite source_name columns
  -> queue_worker request body
  -> DynamoDB source_name attribute
```

If the Meshtastic packet does not include a user name, the final cloud record will also have no `source_name`.

## 14.2 `msg_id` behavior

`msg_id` is still useful, but its role is different from `source_ts_utc`.

### `msg_id`
Useful for:

- tracing sender behavior
- comparing sender-side counters to cloud history
- debugging skipped or rate-limited packets
- correlating logs

### `source_ts_utc`
Used for:

- local weather uniqueness
- cloud observation identity
- time-range queries
- idempotent retries

## 14.3 Why sender and cloud counts can drift

A fast upstream producer can emit packets every few seconds, while the garden-side bridge only forwards one weather reading per minute.

Because of that:

- upstream debug counters can rise quickly
- sender-side `msg_id` can reflect only forwarded traffic
- cloud observation counts track accepted forwarded observations, not raw upstream UDP volume

That behavior is expected in the current system.

---

# 15. Practical summary of each table and item type

## SQLite tables

### `weather_readings`
Canonical local weather history.

### `aws_delivery_queue`
Persistent backlog and retry state for AWS delivery.

### `device_health_events`
Local history of health/debug/status packets.

### `device_status_current`
One-row-per-device latest state snapshot.

### `ingest_events`
Diagnostics ledger for invalid, rejected, unknown, or duplicate packets.

## DynamoDB item types

### Observation item
Canonical cloud history record for one weather observation.

### Latest item
Current snapshot for one station.

---

# 16. Bottom line

The MVP is built around a solid pattern:

- capture locally first
- queue for cloud delivery
- retry until successful
- store durable cloud history
- expose latest and history through simple read APIs

The home-side SQLite schema and `storage.py` implementation make the Pi resilient to network outages and repeated packets. The AWS side keeps the cloud model simple by storing:

- one historical observation item per `(station, source timestamp, record type)`
- one latest snapshot item per station

The most important system-wide design choice is that **weather identity is timestamp-based**, not message-counter-based. That decision now lines up across both SQLite and DynamoDB, which makes deduplication, retry behavior, and time-range querying much easier to reason about.
