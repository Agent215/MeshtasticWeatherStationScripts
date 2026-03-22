# Weather Station Scripts

This repository contains the current weather-station MVP documentation plus the code artifacts that support the garden bridge, Meshtastic ingest, AWS APIs, and local test tooling.

The production system is a store-and-forward pipeline:

`weather source -> garden bridge -> Meshtastic -> home Raspberry Pi -> SQLite backlog -> AWS ingest API -> DynamoDB -> read APIs`

The primary production reference is [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md). That document describes the current home-server schema, queueing model, AWS stack, and DynamoDB data model. The checked-in code in this repository covers the garden bridge, a Meshtastic listener utility, the AWS Lambda handlers, and mocks used for bench testing.

## Project Links

- Hackaday project page: https://hackaday.io/project/205363-meshtastic-weather-station
- Live demo: https://brahmschultz.com/meshtastic-weather-station

## Production Overview

- Garden-side Tempest-style weather packets are received over local UDP, normalized on the Pico, and forwarded over UART into a Meshtastic node.
- The home side is the durable ingest point in production: accepted packets are stored in SQLite, queued for AWS delivery, and retried until they succeed.
- AWS stores historical observations and a latest snapshot in DynamoDB behind API Gateway and Lambda.
- System-wide weather identity is timestamp-based: `(source_node_id, source_ts_utc)`.

## Repository Layout

- [`gardenNode/main.py`](./gardenNode/main.py): MicroPython garden bridge for the Raspberry Pi Pico W
- [`util/home_server_listen_meshtastic.py`](./util/home_server_listen_meshtastic.py): home-side Meshtastic USB listener/logger utility
- [`util/tempest_udp_listener_test_script.py`](./util/tempest_udp_listener_test_script.py): UDP listener/validator for supported Tempest packet types
- [`aws/ingest/app.py`](./aws/ingest/app.py): ingest Lambda for `POST /observations`
- [`aws/read/app.py`](./aws/read/app.py): read Lambda for `GET /observations` and `GET /observations/latest`
- [`aws/weather-station-stack.yaml`](./aws/weather-station-stack.yaml): source CloudFormation template
- [`aws/packaged-template.yaml`](./aws/packaged-template.yaml): packaged CloudFormation template
- [`mocks/mock_tempest_udp_sender.py`](./mocks/mock_tempest_udp_sender.py): mock Tempest `obs_st` UDP sender
- [`mocks/mock_tempest_udp_sender_extended.py`](./mocks/mock_tempest_udp_sender_extended.py): mock Tempest sender for `obs_st`, `evt_precip`, `evt_strike`, `device_status`, and `hub_status`
- [`mocks/ecowitt_mock_server_v3.py`](./mocks/ecowitt_mock_server_v3.py): mock Ecowitt LAN API server for local integration work
- [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md): current production architecture and schema
- [`documentation/markdown/weather_station_design_revised_v2.md`](./documentation/markdown/weather_station_design_revised_v2.md): broader system design background
- [`documentation/markdown/home_pi_server_design_spec.md`](./documentation/markdown/home_pi_server_design_spec.md): earlier home-server design document

## Garden Bridge

[`gardenNode/main.py`](./gardenNode/main.py) runs on a Raspberry Pi Pico W under MicroPython and currently implements the garden-side production bridge.

It does the following:

- connects to local Wi-Fi and listens for Tempest-style UDP packets on port `50222`
- supports `obs_st`, `evt_precip`, `evt_strike`, `device_status`, and `hub_status`
- validates payload structure, sanity-checks supported field ranges, and rounds weather values before forwarding
- forwards compact newline-delimited JSON over UART at `115200` baud
- uses `GP0` for TX and `GP1` for RX
- applies per-type forwarding throttles:
  - `obs_st`: 60 seconds
  - `evt_precip`: 60 seconds
  - `evt_strike`: 30 seconds
  - `device_status`: 60 seconds
  - `hub_status`: 60 seconds
- enforces a minimum 5-second gap between any two UART sends
- keeps a small priority outbound queue (`MAX_OUTBOUND_QUEUE = 12`) and replaces the latest queued `obs_st`, `device_status`, and `hub_status` snapshot instead of endlessly stacking stale copies
- emits a `sys="dbg"` heartbeat every 15 minutes
- performs staged recovery when UDP traffic stops arriving:
  - recreate the UDP socket after 120 no-UDP cycles
  - force a Wi-Fi reconnect after 300 no-UDP cycles
  - reboot is effectively disabled in the current build (`NO_UDP_REBOOT_THRESHOLD = 999999`)

Current `obs_st` UART payload shape:

```json
{"et":"obs_st","i":17,"ts":1741985112,"t":22.5,"h":52,"p":1011.4,"w":3.4,"g":5.2,"l":1.1,"d":230,"r":0.0,"uv":2.4,"sr":410.0,"lux":12500,"bat":2.48,"ld":0.0,"lc":0,"pt":0,"ri":1,"rd":0.0,"nr":0.0,"nrd":0.0,"pa":0}
```

Other forwarded UART payload types use these field sets:

- `evt_precip`: `et`, `i`, `ts`
- `evt_strike`: `et`, `i`, `ts`, `ld`, `se`
- `device_status`: `et`, `i`, `ts`, `up`, `v`, `fw`, `r`, `hr`, `ss`, `dbg`
- `hub_status`: `et`, `i`, `ts`, `up`, `fw`, `r`, `rf`, `seq`, `fs`, `rs`, `ms`

Current heartbeat payload shape:

```json
{"sys":"dbg","i":18,"up":900,"ip":"192.168.1.205","wc":1,"pm":0,"udp":54,"jerr":0,"unsup":0,"rej":0,"skip":12,"qsz":1,"qrepl":4,"qdrop":0,"fwd":22,"sockrec":0,"wifirec":0,"sockerr":0,"nwu":0,"last_udp_s":2,"last_obs_s":2,"last_ok_s":61}
```

## Hardware And Interface Notes

The current production-oriented hardware layout is:

- WeatherFlow Tempest sensor and hub
- a small garden 2.4 GHz router/LAN
- Raspberry Pi Pico W in the garden
- garden and home RAK Meshtastic nodes
- a home Raspberry Pi connected to the home node by USB
- a 12 V solar/battery system in the garden with a regulated 5 V rail

Important garden-side assumptions:

- the Tempest hub and Pico W must be on the same local Wi-Fi network
- the Pico listens for local UDP broadcast traffic on port `50222`
- internet access is not required for local UDP collection
- the Pico forwards newline-delimited JSON over UART into the garden Meshtastic node

Pico UART details from the current bridge code:

- `GP0` = TX
- `GP1` = RX
- `115200` baud
- 3.3 V logic only

Recommended Meshtastic serial settings from the existing project notes:

```text
Serial enabled: ON
Echo enabled: ON
RX: 15
TX: 16
Serial baud rate: 115200
Timeout: 0
Serial mode: TEXTMSG
Override console serial port: OFF
```

Recommended Pico-to-RAK wiring:

```text
Pico GP0 (TX) -> RAK RX1
Pico GP1 (RX) -> RAK TX1
Pico GND      -> RAK GND
```

Additional hardware notes that matter in practice:

- do not feed 5 V UART logic into the RAK UART pins
- connect the home Meshtastic node to the Raspberry Pi by USB for the most stable long-running gateway link
- both Meshtastic radios should be configured for the same region and channel/PSK; the earlier design notes assume `US915`
- the broader hardware and power plan, including antenna and solar/battery notes, is documented in [`documentation/markdown/weather_station_design_revised_v2.md`](./documentation/markdown/weather_station_design_revised_v2.md)

## Home Side And AWS

[`util/home_server_listen_meshtastic.py`](./util/home_server_listen_meshtastic.py) is the checked-in home-side listener utility. It connects to a Meshtastic node over USB serial, emits structured JSON logs for packet/text/connection events, and automatically reconnects if the serial link drops. It uses the `MESHTASTIC_DEVICE` environment variable to target a specific serial device.

The current production home-server architecture is documented in [`documentation/markdown/weather_station_mvp_architecture_and_schema.md`](./documentation/markdown/weather_station_mvp_architecture_and_schema.md). That document defines the SQLite-backed ingest pipeline, including the production `parser.py`, `storage.py`, `db.py`, `schema.sql`, and `queue_worker.py` modules. Those home-server modules are described in the architecture doc but are not currently checked into this repository.

The AWS side in this repository matches the production design:

- [`aws/ingest/app.py`](./aws/ingest/app.py) accepts `POST /observations`, requires the `x-weatherstation-key` header, normalizes `source_ts_utc` and `received_at_utc` from ISO-8601 or epoch input, validates known `weather` fields, writes history rows idempotently, and only advances the `LATEST` snapshot when the incoming observation is newer.
- [`aws/read/app.py`](./aws/read/app.py) serves `GET /observations/latest?stationId=...` and `GET /observations?stationId=...&from=...&to=...`, with support for `limit`, `nextToken`, `order`, and optional evenly sampled history via `sample`.
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

Send basic `obs_st` Tempest-style UDP packets to the Pico:

```powershell
python .\mocks\mock_tempest_udp_sender.py --target 192.168.1.205 --interval 10
```

Or broadcast on the LAN:

```powershell
python .\mocks\mock_tempest_udp_sender.py
```

Simulate the full supported Tempest packet mix:

```powershell
python .\mocks\mock_tempest_udp_sender_extended.py --target 192.168.1.205
```

Validate incoming Tempest UDP payloads on a workstation or Pi:

```powershell
python .\util\tempest_udp_listener_test_script.py --port 50222
```

Run the Ecowitt LAN API mock server for local integration work:

```powershell
python .\mocks\ecowitt_mock_server_v3.py --host 127.0.0.1 --port 8080
```

### Home listener utility

1. Install the Python dependencies used by [`util/home_server_listen_meshtastic.py`](./util/home_server_listen_meshtastic.py), including `meshtastic` and `pypubsub`.
2. Set `MESHTASTIC_DEVICE` if you want to target a specific serial path.
3. Run:

```powershell
python .\util\home_server_listen_meshtastic.py
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
- The repository currently includes the garden bridge, a home Meshtastic listener utility, AWS ingest/read Lambdas, infrastructure templates, and local validation/mock scripts.
- Some production home-server modules described in the architecture document are not currently present in this repository.
- There is currently no automated test suite in this repository; validation is script-based.

## License

This project is licensed under the terms in [`LICENSE`](./LICENSE).
