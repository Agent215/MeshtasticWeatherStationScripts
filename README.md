# Tempest UDP -> Meshtastic UART Bridge (Pico W)

Resilience-focused MicroPython bridge for Raspberry Pi Pico W:

- listens for WeatherFlow Tempest UDP broadcast packets on port `50222`
- validates/parses `obs_st` packets
- emits compact newline-delimited JSON over UART to a Meshtastic-connected device
- survives malformed data, Wi-Fi outages, transient UART failures, and subsystem exceptions

## Code structure

- `main.py`: thin hardware boot entrypoint for Pico W.
- `weather_bridge/core.py`: pure parsing/validation/payload formatting logic.
- `weather_bridge/retry.py`: simple bounded exponential backoff policy.
- `weather_bridge/state_machine.py`: explicit runtime states.
- `weather_bridge/diagnostics.py`: structured counters and event log snapshots.
- `weather_bridge/adapters.py`: hardware adapters (Wi-Fi, UDP, UART, clock) + abstractions.
- `weather_bridge/runtime.py`: orchestration loop, fault containment, recovery behavior, fault injection hooks.

## Runtime state machine

States:

- `BOOTING`
- `WIFI_CONNECTING`
- `UDP_READY`
- `RECOVERING`
- `FATAL_RESTART_PENDING`

High-level flow:

1. Boot and attempt Wi-Fi connect.
2. Move to `UDP_READY` when connected.
3. Receive UDP; parse only valid `obs_st` packets.
4. Forward weather payloads over UART with rate-limiting and dedup.
5. Send idle heartbeats periodically.
6. On exception, enter `RECOVERING`, reopen UDP, and apply backoff.
7. If failures exceed threshold, move to `FATAL_RESTART_PENDING` and reset.

## Fault injection hooks

`FaultInjector` in `weather_bridge/runtime.py` supports simulation of:

- Wi-Fi loss (`force_wifi_loss`)
- malformed UDP (`force_malformed_udp`)
- socket exceptions (`force_socket_exception`)
- UART write exceptions (`force_uart_exception`)
- memory pressure (`force_memory_pressure`)
- forced main-loop exception (`force_main_loop_exception`)

## Diagnostics counters/logging

`Diagnostics.counters` includes:

- Wi-Fi connect attempts/failures
- UDP received/malformed/socket errors
- UART sent/errors
- heartbeat sent
- recoveries
- top-level exceptions
- memory pressure events

`Diagnostics.events` stores structured event records (recent tail kept in snapshot).

## Host-side tests (desktop Python)

### Install and run

```bash
python -m pip install -U pytest
pytest -q
```

### Coverage focus

- packet parsing (`tests/test_unit_logic.py`)
- malformed packet handling (`tests/test_unit_logic.py`, `tests/test_component_runtime.py`)
- payload construction (`tests/test_unit_logic.py`)
- retry/backoff logic (`tests/test_unit_logic.py`)
- state machine transitions (`tests/test_unit_logic.py`)
- exception containment (`tests/test_component_runtime.py`)
- component behavior with fake adapters (`tests/test_component_runtime.py`, `tests/fakes.py`)

## Device-side test scripts (mpremote)

Scripts in `device_tests/`:

- `test_wifi_reconnect.py`
- `test_udp_receive.py`
- `test_malformed_tolerance.py`
- `test_uart_send.py`
- `test_idle_heartbeat.py`
- `test_top_level_recovery.py`

### Copy and run on Pico

```bash
mpremote fs cp -r weather_bridge :weather_bridge
mpremote fs cp -r device_tests :device_tests
mpremote run device_tests/test_wifi_reconnect.py
mpremote run device_tests/test_udp_receive.py
mpremote run device_tests/test_malformed_tolerance.py
mpremote run device_tests/test_uart_send.py
mpremote run device_tests/test_idle_heartbeat.py
mpremote run device_tests/test_top_level_recovery.py
```

Each script prints `PASS <name>` or `FAIL <name> <error>`.

## Test matrix and pass/fail criteria

| Area | Host unit | Host component | Device script | Pass criteria |
|---|---|---|---|---|
| Packet parsing | ✅ | ✅ | `test_udp_receive.py` | valid `obs_st` produces compact weather payload |
| Malformed handling | ✅ | ✅ | `test_malformed_tolerance.py` | malformed packet increments malformed counter, loop keeps running |
| Payload construction | ✅ | ✅ | `test_uart_send.py` | newline-delimited compact JSON over UART |
| Retry/backoff | ✅ | ✅ | `test_wifi_reconnect.py` | reconnect retries, backoff/recovery state entered |
| State machine | ✅ | ✅ | `test_top_level_recovery.py` | transitions among BOOTING/WIFI_CONNECTING/UDP_READY/RECOVERING/FATAL |
| Exception containment | ✅ | ✅ | `test_top_level_recovery.py` | exceptions do not hang process; recovery/fatal path taken |
| Idle heartbeat | (via component) | ✅ | `test_idle_heartbeat.py` | heartbeat emitted when no UDP traffic |

## Recovery behavior by fault class

- **Power failure / abrupt reboot**: startup is stateless and idempotent; runtime re-enters Wi-Fi connect and UDP ready flow automatically.
- **Malformed packets**: parser returns `None`; packet is counted as malformed and ignored.
- **No UDP traffic**: socket timeout is treated as normal; loop continues and heartbeat still emits.
- **Wi-Fi outage**: transitions to `WIFI_CONNECTING`/`RECOVERING`; retries with bounded exponential backoff.
- **Repeated UART errors**: enters recovery path, increments counters, retries loop; escalates to fatal reset threshold.
- **Unexpected subsystem exceptions**: caught at top-level tick, logged as structured recovery events; UDP reopened and backoff applied.

## Hardware notes

- Tempest hub and Pico W must share the same LAN.
- UART uses 3.3V logic and shared ground.
- Default UART pins in `main.py`: `GP0` TX, `GP1` RX.
