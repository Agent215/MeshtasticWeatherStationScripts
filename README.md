# Weather Station Scripts

This repository contains the garden-side bridge code for a self-hosted weather station and the current design documents for the full end-to-end system.

The implemented code today focuses on the Raspberry Pi Pico W in the garden:

- Listen for local WeatherFlow Tempest UDP broadcasts on the garden LAN
- Extract the key `obs_st` weather fields
- Validate and compact the payload
- Forward the result over UART to a Meshtastic/RAK node
- Emit periodic heartbeat/status messages

The broader system described in the documentation extends that path to a home Meshtastic node, a Raspberry Pi gateway, local SQLite storage, and AWS delivery.

## System Overview

Planned message flow:

`Tempest sensor -> Tempest hub -> garden Wi-Fi LAN -> Pico W -> UART -> garden RAK node -> Meshtastic mesh -> home RAK node -> Raspberry Pi -> SQLite -> AWS`

Current code in this repo implements the `garden Wi-Fi LAN -> Pico W -> UART` portion and includes a mock UDP sender for bench testing.

## Repository Contents

- [`main.py`](./main.py): MicroPython application for the Raspberry Pi Pico W
- [`mock_tempest_sender.py`](./mock_tempest_sender.py): desktop test utility that sends Tempest-style `obs_st` UDP packets
- [`documentation/markdown/weather_station_design_revised_v2.md`](./documentation/markdown/weather_station_design_revised_v2.md): overall system architecture, hardware, interfaces, power, bring-up plan, and AWS overview
- [`documentation/markdown/home_pi_server_design_spec.md`](./documentation/markdown/home_pi_server_design_spec.md): planned Raspberry Pi home gateway design, packet handling, SQLite schema, retry behavior, and deployment notes

## Implemented Script Behavior

### `main.py`

`main.py` is intended to run on a Raspberry Pi Pico W under MicroPython.

It does the following:

- Connects the Pico W to the local garden Wi-Fi network
- Binds a UDP listener on port `50222`
- Accepts Tempest `obs_st` packets and ignores other packet types
- Extracts these fields from the first observation record:
  - timestamp
  - temperature
  - humidity
  - pressure
  - average wind speed
  - wind direction
  - rain accumulation for the interval
- Rejects malformed or out-of-range readings
- Suppresses duplicate or stale observations using the source timestamp
- Limits forwarding to one weather message per 60 seconds
- Sends compact newline-delimited JSON over UART at `115200` baud
- Sends a heartbeat/status JSON message every 6 hours
- Attempts Wi-Fi recovery and reboots the Pico after repeated unrecoverable failures

Weather payload sent over UART:

```json
{"i":17,"t":22.5,"h":52,"p":1011.4,"w":3.4,"d":230,"r":0.0}
```

Heartbeat payload sent over UART:

```json
{"sys":"ok","i":18,"up":21600,"ip":"192.168.1.205"}
```

### `mock_tempest_sender.py`

This script is a host-side test tool for bench work and LAN validation.

It does the following:

- Generates realistic-looking Tempest `obs_st` UDP payloads
- Sends them to a target IP and port at a configurable interval
- Defaults to UDP broadcast on `255.255.255.255:50222`
- Can also send directly to the Pico W IP for unicast testing

Example:

```powershell
python mock_tempest_sender.py --target 192.168.1.205 --interval 10
```

Or broadcast on the local LAN:

```powershell
python mock_tempest_sender.py
```

## Hardware and Interface Notes

Based on the code and design docs, the important garden-side assumptions are:

- The Tempest hub and Pico W must be on the same local Wi-Fi network
- Tempest data is consumed from local UDP broadcast on port `50222`
- Pico W UART is configured for:
  - `GP0` as TX
  - `GP1` as RX
  - `115200` baud
- UART wiring to the RAK/Meshtastic node should use 3.3 V logic and shared ground
- Do not feed 5 V UART logic into the RAK UART pins

## Setup

### Pico W bridge

1. Install MicroPython on the Raspberry Pi Pico W.
2. Edit [`main.py`](./main.py) and set:
   - `WIFI_SSID`
   - `WIFI_PASSWORD`
3. Copy `main.py` to the Pico as the boot script.
4. Wire the Pico to the garden RAK node:
   - Pico `GP0` TX -> RAK RX
   - Pico `GP1` RX -> RAK TX
   - GND -> GND
5. Power the Pico and confirm it joins Wi-Fi and starts listening on UDP port `50222`.

### Mock testing

1. Put the Pico W on the same LAN as the machine running the mock sender.
2. Start the Pico bridge.
3. Run [`mock_tempest_sender.py`](./mock_tempest_sender.py) from a desktop Python environment.
4. Confirm the Pico logs parsed `obs_st` packets and forwards compact JSON over UART.

## Documentation Summary

### Overall system design

[`documentation/markdown/weather_station_design_revised_v2.md`](./documentation/markdown/weather_station_design_revised_v2.md) defines the recommended full architecture:

- Tempest sensor and hub in the garden
- Private garden Wi-Fi LAN
- Pico W as the local UDP-to-UART bridge
- Garden and home Meshtastic nodes using US915 LoRa
- Raspberry Pi home gateway over USB
- AWS backend using API Gateway, Lambda, and DynamoDB
- 12 V solar/battery power with a regulated 5 V rail for garden electronics

### Planned home gateway

[`documentation/markdown/home_pi_server_design_spec.md`](./documentation/markdown/home_pi_server_design_spec.md) describes the not-yet-implemented home-side service:

- USB serial ingest from the home Meshtastic node
- Packet classification into weather vs. health/status
- SQLite as the local system of record
- Durable AWS retry queue
- Derived node state such as `healthy`, `degraded`, `offline`, and `bad_health`
- `systemd` deployment on a Raspberry Pi 5

## Current Project Status

Implemented in this repo:

- Pico W UDP listener and UART bridge
- Compact outbound weather payload format
- Heartbeat/status payloads from the Pico
- Mock Tempest UDP sender for testing

Specified in docs but not implemented here yet:

- Raspberry Pi home gateway service
- SQLite storage and deduplication layer
- AWS delivery worker and retry queue
- Website or dashboard

## Notes

- `main.py` is MicroPython code for the Pico W, not standard desktop Python.
- `mock_tempest_sender.py` is standard Python intended to run on a laptop or desktop during testing.
- There are currently no automated tests in this repository.

## License

This project is licensed under the terms in [`LICENSE`](./LICENSE).
