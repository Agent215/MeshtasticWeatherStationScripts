# Home Raspberry Pi 5 Server Design Specification

*Meshtastic Serial Ingest | SQLite Persistence | AWS API Delivery*

This document is an implementation-aligned overview of the checked-in home-server code in [`weatherstation/`](../../weatherstation). For the exhaustive schema and full end-to-end cloud model, use [`weather_station_mvp_architecture_and_schema.md`](../architecture/weather_station_mvp_architecture_and_schema.md).

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

- [`weatherstation/listen_meshtastic.py`](../../weatherstation/listen_meshtastic.py): serial ingest, parsing, and SQLite writes
- [`weatherstation/queue_worker.py`](../../weatherstation/queue_worker.py): AWS delivery worker for queued weather rows

High-level data path:

```text
garden weather source
  -> garden Pico bridge
  -> Meshtastic text message
  -> home Meshtastic node over USB serial
  -> weatherstation/listen_meshtastic.py
  -> weatherstation/parser.py
  -> weatherstation/storage.py
  -> SQLite
  -> weatherstation/queue_worker.py
  -> AWS POST /observations
```

Supporting files:

- [`weatherstation/app_config.py`](../../weatherstation/app_config.py): shared environment-file and typed setting loader
- [`weatherstation/db.py`](../../weatherstation/db.py): SQLite connection helper
- [`weatherstation/schema.sql`](../../weatherstation/schema.sql): schema definition
- [`weatherstation/retention.py`](../../weatherstation/retention.py): bounded SQLite retention cleanup job
- [`weatherstation/commands.txt`](../../weatherstation/commands.txt): operational command reference used on the Pi
- [`scripts/home/test_ingest.py`](../../scripts/home/test_ingest.py): simple local ingest smoke script
- [`scripts/home/show_latest.py`](../../scripts/home/show_latest.py): quick SQLite inspection helper
- [`scripts/home/meshtastic_debug_logger.py`](../../scripts/home/meshtastic_debug_logger.py): standalone Meshtastic debug logger, not the production SQLite/AWS pipeline

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

The default database path in code is:

```text
~/weatherstation-home/weatherstation/weatherstation.db
```

`db.py` also supports `WEATHERSTATION_DB_PATH` when the deployed database lives
somewhere else.

`db.py` opens SQLite with:

- `PRAGMA foreign_keys=ON`
- `PRAGMA journal_mode=WAL`
- `PRAGMA busy_timeout=<timeout_sec * 1000>`
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

- `API_URL` and `API_KEY` are loaded through `weatherstation/app_config.py`
- config resolution prefers `~/weatherstation-home/.env`
- `WEATHERSTATION_ENV_PATH` can point at a different env file
- `/etc/weatherstation-home.env` is used as a fallback when present
- `MESHTASTIC_DEVICE` is optional and used by `listen_meshtastic.py`
- `MESHTASTIC_WATCHDOG_ENABLED`, `MESHTASTIC_WATCHDOG_TIMEOUT_SEC`, and `MESHTASTIC_RECONNECT_DELAY_SEC` control the listener reconnect watchdog

In the live home-server setup, the listener-specific Meshtastic settings are
typically managed in:

```text
/etc/weatherstation-meshtastic.env
```

while queue-worker and retention settings are typically managed in:

```text
/etc/weatherstation-home.env
```

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

# 6. Retention And Cleanup

`retention.py` is the current production cleanup job for the local SQLite
database.

It:

- deletes expired rows from `weather_readings`, `device_health_events`,
  `weather_events`, `device_telemetry_events`, and `ingest_events`
- never deletes `device_status_current`
- only deletes `weather_readings` when the matching queue row is absent or
  already marked `delivered`
- runs in bounded batches so the database does not hold long write locks
- runs `PRAGMA optimize` after live cleanup and performs a passive WAL
  checkpoint when rows were deleted

Retention is controlled with these environment variables:

- `DB_RETENTION_ENABLED`
- `DB_RETENTION_DAYS`
- `DB_RETENTION_BATCH_SIZE`
- `DB_RETENTION_MAX_BATCHES`

Operational reference copies of the live retention units are checked into
[`docs/systemd/`](../systemd/).

# 7. Deployment Notes

The code assumes a deployed layout like:

```text
~/weatherstation-home/
  .env
  weatherstation/
    app_config.py
    listen_meshtastic.py
    queue_worker.py
    parser.py
    storage.py
    db.py
    schema.sql
    retention.py
```

Initialize the database once before starting the services:

```bash
sqlite3 ~/weatherstation-home/weatherstation/weatherstation.db < ~/weatherstation-home/weatherstation/schema.sql
```

Typical service model:

- one systemd unit for `listen_meshtastic.py`
- one systemd unit for `queue_worker.py`
- one daily `weatherstation-db-retention.timer` for `retention.py` when local
  history cleanup is enabled

Example listener unit:

```ini
[Unit]
Description=Weather Station Meshtastic Listener
After=network-online.target
Wants=network-online.target

[Service]
User=gardener
Group=gardener
WorkingDirectory=/home/gardener/weatherstation-home/weatherstation
EnvironmentFile=/etc/weatherstation-meshtastic.env
ExecStart=/home/gardener/weatherstation-home/.venv/bin/python /home/gardener/weatherstation-home/weatherstation/listen_meshtastic.py
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
User=gardener
Group=gardener
WorkingDirectory=/home/gardener/weatherstation-home/weatherstation
Type=notify
EnvironmentFile=/etc/weatherstation-home.env
ExecStart=/home/gardener/weatherstation-home/.venv/bin/python /home/gardener/weatherstation-home/weatherstation/queue_worker.py
Restart=always
RestartSec=5
WatchdogSec=30

[Install]
WantedBy=multi-user.target
```

The example units above intentionally match the current live deployment path on
the home server:

```text
/home/gardener/weatherstation-home
```

# 8. Observability and Validation

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
- `watchdog_timeout`
- `reconnect_scheduled`
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

Representative retention events:

- `retention_start`
- `retention_table_dry_run`
- `retention_batch_deleted`
- `retention_table_complete`
- `retention_complete`

Validation in this repository is still script-based rather than test-suite-based.

# 9. Current Limitations

The checked-in implementation intentionally does not do the following yet:

- compute or persist a derived health state in `device_status_current.derived_state`
- upload `sys`, `evt_precip`, `evt_strike`, `device_status`, or `hub_status` packets to AWS
- ingest native Meshtastic `TELEMETRY_APP` packets into SQLite
- provide an automated test suite
