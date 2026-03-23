# Home Raspberry Pi 5 Server Design Specification

*Meshtastic Serial Ingest | SQLite Persistence | AWS API Delivery*

This document is an implementation-aligned overview of the checked-in home-server code in [`homeServer/`](../../homeServer). For the exhaustive schema and full end-to-end cloud model, use [`weather_station_mvp_architecture_and_schema.md`](./weather_station_mvp_architecture_and_schema.md).

# 1. Purpose and Scope

The home Raspberry Pi is the durable ingest point between the local Meshtastic mesh and AWS. It accepts text payloads from the USB-connected home node, stores accepted data locally in SQLite, and forwards weather observations to the AWS ingest API with at-least-once delivery semantics.

In scope in the current code:

- Meshtastic serial ingest from the home node
- packet parsing and validation
- SQLite persistence
- local event, telemetry, and ingest-diagnostics history
- durable AWS weather-delivery queue
- structured JSON logging
- long-running deployment under systemd or equivalent service supervision

Out of scope in the current code:

- Home Assistant integration
- local dashboard UI
- bidirectional commands back into the mesh
- automatic `derived_state` computation
- cloud delivery of health, event, or telemetry packets

# 2. Runtime Architecture

The checked-in implementation is two cooperating long-lived processes, not one threaded process:

- [`homeServer/listen_meshtastic.py`](../../homeServer/listen_meshtastic.py): serial ingest, parsing, and SQLite writes
- [`homeServer/queue_worker.py`](../../homeServer/queue_worker.py): AWS delivery worker for queued weather rows

High-level data path:

```text
garden weather source
  -> garden Pico bridge
  -> Meshtastic text message
  -> home Meshtastic node over USB serial
  -> homeServer/listen_meshtastic.py
  -> homeServer/parser.py
  -> homeServer/storage.py
  -> SQLite
  -> homeServer/queue_worker.py
  -> AWS POST /observations
```

Supporting files:

- [`homeServer/db.py`](../../homeServer/db.py): SQLite connection helper
- [`homeServer/schema.sql`](../../homeServer/schema.sql): schema definition
- [`homeServer/util/test_ingest.py`](../../homeServer/util/test_ingest.py): simple local ingest smoke script
- [`homeServer/util/show_latest.py`](../../homeServer/util/show_latest.py): quick SQLite inspection helper
- [`util/home_server_listen_meshtastic.py`](../../util/home_server_listen_meshtastic.py): standalone Meshtastic logger/debug utility, not the production SQLite/AWS pipeline

# 3. Packet Classification

The current listener only parses `TEXT_MESSAGE_APP` packets. Native Meshtastic `TELEMETRY_APP` packets are logged as `telemetry_packet_seen` but are not stored in SQLite.

Text payload handling:

| Payload shape | Parser result | Storage action | AWS delivery |
| :---- | :---- | :---- | :---- |
| `et="obs_st"` or legacy compact weather payload with required weather keys and `ts` | `weather` | insert `weather_readings`, enqueue `aws_delivery_queue`, update `device_status_current` | Yes |
| `sys=...` heartbeat/debug packet | `health` | insert `device_health_events`, update `device_status_current` | No |
| `et="evt_precip"` or `et="evt_strike"` | `weather_event` | insert `weather_events` | No |
| `et="device_status"` or `et="hub_status"` | `telemetry` | insert `device_telemetry_events`, update `device_status_current` | No |
| malformed JSON | `invalid` | insert `ingest_events` | No |
| known schema with invalid values | `rejected` | insert `ingest_events` | No |
| unknown JSON object | `unknown` | insert `ingest_events` | No |

Important parser details:

- accepted weather, weather-event, and telemetry packets require `ts`
- health packets can omit `ts`
- `source_ts_utc` is stored as the raw source timestamp text from the packet
- weather uniqueness is `UNIQUE(source_node_id, source_ts_utc)` in SQLite

# 4. Local Storage Model

The database path is fixed in code:

```text
~/weatherstation-home/weatherstation/weatherstation.db
```

`db.py` opens SQLite with:

- `PRAGMA foreign_keys=ON`
- `PRAGMA journal_mode=WAL`
- `sqlite3.Row` row objects

The checked-in schema contains these tables:

- `weather_readings`: canonical accepted weather history
- `aws_delivery_queue`: durable backlog and retry state for AWS weather delivery
- `device_health_events`: local history for `sys=...` heartbeat/debug packets
- `weather_events`: local history for `evt_precip` and `evt_strike`
- `device_telemetry_events`: local history for `device_status` and `hub_status`
- `device_status_current`: one-row-per-device latest snapshot assembled from weather, health, and telemetry updates
- `ingest_events`: diagnostics ledger for invalid, rejected, unknown, or duplicate packets

Current snapshot behavior:

- weather inserts update `last_weather_at_utc`
- health inserts update `last_health_at_utc` and heartbeat fields
- `device_status` inserts update device-side telemetry fields
- `hub_status` inserts update hub-side telemetry fields
- `derived_state` exists in schema but is not populated by the checked-in code

# 5. AWS Delivery Behavior

Only rows from `weather_readings` are queued for cloud delivery.

`queue_worker.py`:

- polls `aws_delivery_queue` every 5 seconds
- reads due rows with status `pending` or `retry`
- builds a `POST /observations` body with expanded cloud field names
- sends the shared secret in `x-weatherstation-key`
- marks successful rows `delivered`
- marks failed rows `retry` and schedules the next persistent attempt

Configuration:

- `API_URL` and `API_KEY` are loaded from the `.env` file one directory above `queue_worker.py`
- in the deployed Pi layout that path is `~/weatherstation-home/.env`
- `MESHTASTIC_DEVICE` is optional and used by `listen_meshtastic.py`

Retry behavior:

- in-process HTTP retry attempts: `5`
- immediate retry delays: exponential from `2` seconds, capped at `30` seconds
- persistent SQLite retry schedule after a failed queue cycle:
  - first failure: `30` seconds
  - second failure: `120` seconds
  - third failure: `600` seconds
  - fourth and later failures: `1800` seconds

Current validation mismatch worth documenting:

- the local parser accepts `precipitation_type` values `0` through `4`, while AWS ingest accepts only `0` through `3`
- the local parser accepts `precipitation_analysis_type` values `0` through `10`, while AWS ingest accepts only `0` through `4`

That means some rows can be accepted into local SQLite and later fail cloud delivery until the data or code changes.

# 6. Deployment Notes

The code assumes a deployed layout like:

```text
~/weatherstation-home/
  .env
  weatherstation/
    listen_meshtastic.py
    queue_worker.py
    parser.py
    storage.py
    db.py
    schema.sql
```

Initialize the database once before starting the services:

```bash
sqlite3 ~/weatherstation-home/weatherstation/weatherstation.db < ~/weatherstation-home/weatherstation/schema.sql
```

Typical service model:

- one systemd unit for `listen_meshtastic.py`
- one systemd unit for `queue_worker.py`

Example listener unit:

```ini
[Unit]
Description=Weather Station Meshtastic Listener
After=network-online.target
Wants=network-online.target

[Service]
User=weatherstation
WorkingDirectory=/opt/weatherstation-home
Environment=MESHTASTIC_DEVICE=/dev/ttyACM0
ExecStart=/opt/weatherstation-home/.venv/bin/python /opt/weatherstation-home/weatherstation/listen_meshtastic.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Example queue-worker unit:

```ini
[Unit]
Description=Weather Station Queue Worker
After=network-online.target
Wants=network-online.target

[Service]
User=weatherstation
WorkingDirectory=/opt/weatherstation-home
Type=notify
ExecStart=/opt/weatherstation-home/.venv/bin/python /opt/weatherstation-home/weatherstation/queue_worker.py
Restart=always
RestartSec=5
WatchdogSec=30

[Install]
WantedBy=multi-user.target
```

# 7. Observability and Validation

Both runtime scripts log structured one-line JSON to stdout.

Representative listener events:

- `service_start`
- `connecting`
- `connected`
- `packet_received`
- `text_message_received`
- `weather_saved`
- `weather_duplicate`
- `health_saved`
- `weather_event_saved`
- `telemetry_saved`
- `packet_not_saved`
- `connect_error`
- `meshtastic_connected`
- `meshtastic_disconnected`

Representative queue-worker events:

- `queue_worker_start`
- `delivery_attempt_started`
- `delivery_retry_scheduled`
- `delivery_retry_recovered`
- `delivery_success`
- `delivery_failure`
- `queue_worker_error`
- `queue_worker_stop`

Validation in this repository is still script-based rather than test-suite-based.

# 8. Current Limitations

The checked-in implementation intentionally does not do the following yet:

- compute or persist a derived health state in `device_status_current.derived_state`
- upload `sys`, `evt_precip`, `evt_strike`, `device_status`, or `hub_status` packets to AWS
- ingest native Meshtastic `TELEMETRY_APP` packets into SQLite
- provide an automated test suite
