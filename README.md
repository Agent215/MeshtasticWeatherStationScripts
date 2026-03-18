# Weather Station Scripts

This repository contains the current weather-station MVP documentation plus the code artifacts that support the garden bridge, Meshtastic ingest, AWS APIs, and local test tooling.

The production system is a store-and-forward pipeline:

`weather source -> garden bridge -> Meshtastic -> home Raspberry Pi -> SQLite backlog -> AWS ingest API -> DynamoDB -> read APIs`

The primary production reference is [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md). That document describes the current home-server schema, queueing model, AWS stack, and DynamoDB data model. The checked-in code in this repository covers the garden bridge, a Meshtastic listener utility, the AWS Lambda handlers, and mocks used for bench testing.

## Production Overview

- Garden-side weather data is received over local UDP and forwarded over UART into a Meshtastic node.
- The home side is the durable ingest point in production: accepted packets are stored in SQLite, queued for AWS delivery, and retried until they succeed.
- AWS stores historical observations and a latest snapshot in DynamoDB behind API Gateway and Lambda.
- System-wide weather identity is timestamp-based: `(source_node_id, source_ts_utc)`.

## Repository Layout

- [`gardenNode/main.py`](./gardenNode/main.py): MicroPython garden bridge for the Raspberry Pi Pico W
- [`home_server_listen_meshtastic.py`](./home_server_listen_meshtastic.py): home-side Meshtastic USB listener/logger utility
- [`aws/ingest/app.py`](./aws/ingest/app.py): ingest Lambda for `POST /observations`
- [`aws/read/app.py`](./aws/read/app.py): read Lambda for `GET /observations` and `GET /observations/latest`
- [`aws/weather-station-stack.yaml`](./aws/weather-station-stack.yaml): source CloudFormation template
- [`aws/packaged-template.yaml`](./aws/packaged-template.yaml): packaged CloudFormation template
- [`mocks/mock_tempest_udp_sender.py`](./mocks/mock_tempest_udp_sender.py): mock Tempest `obs_st` UDP sender
- [`mocks/ecowitt_mock_server_v3.py`](./mocks/ecowitt_mock_server_v3.py): mock Ecowitt LAN API server for local integration work
- [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md): current production architecture and schema
- [`documentation/markdown/weather_station_design_revised_v2.md`](./documentation/markdown/weather_station_design_revised_v2.md): broader system design background
- [`documentation/markdown/home_pi_server_design_spec.md`](./documentation/markdown/home_pi_server_design_spec.md): earlier home-server design document

## Garden Bridge

[`gardenNode/main.py`](./gardenNode/main.py) runs on a Raspberry Pi Pico W under MicroPython and currently implements the garden-side production bridge.

It does the following:

- connects to local Wi-Fi and listens for Tempest-style `obs_st` UDP packets on port `50222`
- validates and rounds the incoming observation fields before forwarding
- forwards compact newline-delimited JSON over UART at `115200` baud
- uses `GP0` for TX and `GP1` for RX
- rate-limits weather forwarding to one message every 60 seconds
- emits a `sys="dbg"` heartbeat every 15 minutes
- performs staged recovery when UDP traffic stops arriving:
  - recreate the UDP socket after 120 no-UDP cycles
  - force a Wi-Fi reconnect after 300 no-UDP cycles
  - reboot is effectively disabled in the current build (`NO_UDP_REBOOT_THRESHOLD = 999999`)

Current weather payload shape:

```json
{"i":17,"ts":1741985112,"t":22.5,"h":52,"p":1011.4,"w":3.4,"g":5.2,"l":1.1,"d":230,"r":0.0,"uv":2.4,"sr":410.0,"lux":12500,"bat":2.48,"ld":0.0,"lc":0,"pt":0,"ri":1,"rd":0.0,"nr":0.0,"nrd":0.0,"pa":0}
```

Current heartbeat payload shape:

```json
{"sys":"dbg","i":18,"up":900,"ip":"192.168.1.205","wc":1,"pm":0,"udp":54,"jerr":0,"nobs":0,"rej":0,"skip":44,"sockrec":0,"wifirec":0,"sockerr":0,"nwu":0,"last_udp_s":2,"last_obs_s":2,"last_ok_s":61}
```

## Home Side And AWS

[`home_server_listen_meshtastic.py`](./home_server_listen_meshtastic.py) is the checked-in home-side listener utility. It connects to a Meshtastic node over USB serial, logs packet metadata and decoded text payloads, and automatically reconnects if the serial link drops. It uses the `MESHTASTIC_DEVICE` environment variable to target a specific serial device.

The current production home-server architecture is documented in [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md). That document defines the SQLite-backed ingest pipeline, including the production `parser.py`, `storage.py`, `db.py`, `schema.sql`, and `queue_worker.py` modules. Those home-server modules are described in the architecture doc but are not currently checked into this repository.

The AWS side in this repository matches the production design:

- [`aws/ingest/app.py`](./aws/ingest/app.py) accepts `POST /observations`, requires the `x-weatherstation-key` header, normalizes timestamps, validates the `weather` object, and writes DynamoDB observation items idempotently.
- [`aws/read/app.py`](./aws/read/app.py) serves `GET /observations/latest?stationId=...` and `GET /observations?stationId=...&from=...&to=...`.
- [`aws/weather-station-stack.yaml`](./aws/weather-station-stack.yaml) provisions API Gateway HTTP API, the ingest/read Lambdas, IAM permissions, and the DynamoDB table.

## Data Model

The production data model uses two databases with different roles:

- SQLite on the home server is the operational store for accepted weather packets, health/debug history, ingest diagnostics, and the persistent AWS delivery queue.
- DynamoDB is the cloud serving store for weather history and the latest observation snapshot.

The architecture document defines these SQLite tables:

- `weather_readings`
- `aws_delivery_queue`
- `device_health_events`
- `device_status_current`
- `ingest_events`

The DynamoDB table uses a single-table key design:

- partition key: `pk = STATION#<source_node_id>`
- historical observation key: `sk = OBS#<normalized_source_ts_utc>#WEATHER`
- latest snapshot key: `sk = LATEST`

## Setup

### Garden bridge

1. Edit [`gardenNode/main.py`](./gardenNode/main.py) and set `WIFI_SSID` and `WIFI_PASSWORD`.
2. Copy the script to the Pico W as `main.py`.
3. Configure the garden Meshtastic node serial port for text messages at `115200` baud.
4. Wire the Pico W to the Meshtastic node with shared ground and 3.3 V UART levels:

```text
Pico GP0 (TX) -> RAK RX1
Pico GP1 (RX) -> RAK TX1
Pico GND      -> RAK GND
```

5. Power the Pico and confirm it joins Wi-Fi and starts listening on UDP port `50222`.

### Mock testing

Send Tempest-style UDP packets to the Pico:

```powershell
python .\mocks\mock_tempest_udp_sender.py --target 192.168.1.205 --interval 10
```

Or broadcast on the LAN:

```powershell
python .\mocks\mock_tempest_udp_sender.py
```

### Home listener utility

1. Install the Python dependencies used by [`home_server_listen_meshtastic.py`](./home_server_listen_meshtastic.py), including `meshtastic` and `pypubsub`.
2. Set `MESHTASTIC_DEVICE` if you want to target a specific serial path.
3. Run:

```powershell
python .\home_server_listen_meshtastic.py
```

### AWS stack

Deploy [`aws/weather-station-stack.yaml`](./aws/weather-station-stack.yaml) or [`aws/packaged-template.yaml`](./aws/packaged-template.yaml) with values for:

- `ProjectName`
- `EnvironmentName`
- `ApiSharedSecret`
- `TableName`
- `AllowedCorsOrigin`

## Documentation

- [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md) is the current source of truth for the production home-server and AWS design.
- [`documentation/markdown/weather_station_design_revised_v2.md`](./documentation/markdown/weather_station_design_revised_v2.md) captures the broader system and hardware plan.
- [`documentation/markdown/home_pi_server_design_spec.md`](./documentation/markdown/home_pi_server_design_spec.md) is useful background, but the MVP architecture document supersedes it for the current production schema and cloud flow.

## Current Status

- The production architecture is documented for the full garden -> home server -> AWS path.
- The repository currently includes the garden bridge, a home Meshtastic listener utility, AWS ingest/read Lambdas, infrastructure templates, and mocks.
- Some production home-server modules described in the architecture document are not currently present in this repository.
- There are currently no automated tests in this repository.

## License

This project is licensed under the terms in [`LICENSE`](./LICENSE).
