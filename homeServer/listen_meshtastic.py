#!/usr/bin/env python3
"""Production Meshtastic ingest listener for the home-side server.

This process connects to the USB-attached Meshtastic radio identified by
`MESHTASTIC_DEVICE`, subscribes to packet and connection lifecycle events, and
logs structured JSON records to stdout. Inbound text packets are parsed and
stored in SQLite as weather, health, weather-event, or telemetry records, and
the listener will retry the serial connection if the radio is unplugged, the
link drops, or packet flow stalls long enough to trip the watchdog.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pubsub import pub
import meshtastic.serial_interface

BASE_DIR = Path.home() / "weatherstation-home"
sys.path.insert(0, str(BASE_DIR / "weatherstation"))

from parser import parse_text_payload  # noqa: E402
from storage import (  # noqa: E402
    insert_device_telemetry,
    insert_health,
    insert_weather,
    insert_weather_event,
    record_ingest_event,
)
from app_config import get_bool_env, get_int_env  # noqa: E402

RUNNING = True
DISCONNECT_REQUESTED = threading.Event()
PACKET_ACTIVITY_LOCK = threading.Lock()
LAST_PACKET_MONOTONIC: float | None = None


def mark_packet_activity(seen_at_monotonic: float | None = None) -> None:
    global LAST_PACKET_MONOTONIC

    if seen_at_monotonic is None:
        seen_at_monotonic = time.monotonic()

    with PACKET_ACTIVITY_LOCK:
        LAST_PACKET_MONOTONIC = seen_at_monotonic


def clear_packet_activity() -> None:
    global LAST_PACKET_MONOTONIC

    with PACKET_ACTIVITY_LOCK:
        LAST_PACKET_MONOTONIC = None


def get_packet_idle_seconds(now_monotonic: float | None = None) -> float | None:
    with PACKET_ACTIVITY_LOCK:
        last_packet_monotonic = LAST_PACKET_MONOTONIC

    if last_packet_monotonic is None:
        return None

    if now_monotonic is None:
        now_monotonic = time.monotonic()

    return max(0.0, now_monotonic - last_packet_monotonic)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [make_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def log_event(event: str, **fields: Any) -> None:
    record = {
        "ts": utc_now(),
        "event": event,
        **make_json_safe(fields),
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)


def decode_payload(packet: dict[str, Any]) -> str | None:
    decoded = packet.get("decoded", {})

    text_value = decoded.get("text")
    if isinstance(text_value, str):
        return text_value

    payload = decoded.get("payload")
    if payload is None:
        return None

    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception:
            return repr(payload)

    return str(payload)


def get_source_info(packet: dict[str, Any]) -> tuple[str | None, str | None]:
    from_id = packet.get("fromId")
    from_name = None

    try:
        user = packet.get("user") or {}
        from_name = user.get("longName") or user.get("shortName")
    except Exception:
        pass

    return from_id, from_name


def process_text_packet(
    *,
    packet: dict[str, Any],
    from_id: str | None,
    from_name: str | None,
    text: str | None,
) -> None:
    log_event(
        "text_message_received",
        from_id=from_id,
        from_name=from_name,
        payload=text,
    )

    if not text:
        log_event(
            "ingest_skipped",
            reason="empty_text_payload",
            from_id=from_id,
            from_name=from_name,
        )
        return

    try:
        parsed = parse_text_payload(
            text=text,
            source_node_id=from_id,
            source_name=from_name,
            received_at_utc=utc_now(),
        )
    except Exception as exc:
        log_event(
            "parse_error",
            error=str(exc),
            from_id=from_id,
            from_name=from_name,
            payload=text,
            packet=packet,
        )
        return

    try:
        if parsed.packet_type == "weather":
            result = insert_weather(parsed)
            if result == "duplicate":
                record_ingest_event(parsed)
                log_event(
                    "weather_duplicate",
                    from_id=from_id,
                    from_name=from_name,
                    msg_id=parsed.msg_id,
                )
            else:
                log_event(
                    "weather_saved",
                    from_id=from_id,
                    from_name=from_name,
                    msg_id=parsed.msg_id,
                )

        elif parsed.packet_type == "health":
            insert_health(parsed)
            log_event(
                "health_saved",
                from_id=from_id,
                from_name=from_name,
                msg_id=parsed.msg_id,
            )

        elif parsed.packet_type == "weather_event":
            insert_weather_event(parsed)
            log_event(
                "weather_event_saved",
                from_id=from_id,
                from_name=from_name,
                msg_id=parsed.msg_id,
                event_type=parsed.normalized.get("event_type") if parsed.normalized else None,
            )

        elif parsed.packet_type == "telemetry":
            insert_device_telemetry(parsed)
            log_event(
                "telemetry_saved",
                from_id=from_id,
                from_name=from_name,
                msg_id=parsed.msg_id,
                telemetry_type=parsed.normalized.get("event_type") if parsed.normalized else None,
            )

        else:
            record_ingest_event(parsed)
            log_event(
                "packet_not_saved",
                packet_type=parsed.packet_type,
                reason=parsed.reason,
                from_id=from_id,
                from_name=from_name,
                msg_id=parsed.msg_id,
            )

    except Exception as exc:
        log_event(
            "ingest_error",
            error=str(exc),
            from_id=from_id,
            from_name=from_name,
            payload=text,
            packet=packet,
        )


def on_receive(packet: dict[str, Any], interface: Any) -> None:
    mark_packet_activity()

    from_id, from_name = get_source_info(packet)
    decoded = packet.get("decoded", {})
    portnum = decoded.get("portnum")
    text = decode_payload(packet)
    to_id = packet.get("toId")
    rx_snr = packet.get("rxSnr")
    rx_rssi = packet.get("rxRssi")
    hop_limit = packet.get("hopLimit")
    hop_start = packet.get("hopStart")

    log_event(
        "packet_received",
        from_id=from_id,
        from_name=from_name,
        to_id=to_id,
        portnum=portnum,
        payload=text,
        rx_snr=rx_snr,
        rx_rssi=rx_rssi,
        hop_limit=hop_limit,
        hop_start=hop_start,
    )

    if portnum == "TEXT_MESSAGE_APP":
        process_text_packet(
            packet=packet,
            from_id=from_id,
            from_name=from_name,
            text=text,
        )
    elif portnum == "TELEMETRY_APP":
        log_event(
            "telemetry_packet_seen",
            from_id=from_id,
            from_name=from_name,
        )


def on_connection_established(interface: Any, topic: str = None) -> None:
    DISCONNECT_REQUESTED.clear()
    mark_packet_activity()
    log_event("meshtastic_connected")


def on_connection_lost(interface: Any, topic: str = None) -> None:
    DISCONNECT_REQUESTED.set()
    log_event("meshtastic_disconnected")


def handle_signal(signum: int, frame: Any) -> None:
    global RUNNING
    RUNNING = False
    log_event("shutdown_requested", signal=signum)


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        device = os.environ.get("MESHTASTIC_DEVICE")
        reconnect_delay_sec = get_int_env("MESHTASTIC_RECONNECT_DELAY_SEC", 5, minimum=1)
        watchdog_enabled = get_bool_env("MESHTASTIC_WATCHDOG_ENABLED", True)
        watchdog_timeout_sec = get_int_env("MESHTASTIC_WATCHDOG_TIMEOUT_SEC", 600, minimum=1)
    except RuntimeError as exc:
        log_event("meshtastic_config_error", error=str(exc))
        return 1

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection_established, "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    log_event(
        "service_start",
        device=device,
        reconnect_delay_sec=reconnect_delay_sec,
        watchdog_enabled=watchdog_enabled,
        watchdog_timeout_sec=watchdog_timeout_sec,
    )

    while RUNNING:
        interface = None
        should_retry = False
        try:
            DISCONNECT_REQUESTED.clear()
            clear_packet_activity()
            log_event("connecting", device=device)
            interface = meshtastic.serial_interface.SerialInterface(devPath=device)
            mark_packet_activity()
            log_event("connected", device=device)

            while RUNNING and not DISCONNECT_REQUESTED.wait(timeout=1):
                if not watchdog_enabled:
                    continue

                idle_seconds = get_packet_idle_seconds()
                if idle_seconds is None or idle_seconds < watchdog_timeout_sec:
                    continue

                log_event(
                    "watchdog_timeout",
                    device=device,
                    idle_seconds=round(idle_seconds, 3),
                    timeout_sec=watchdog_timeout_sec,
                )
                should_retry = RUNNING
                break

            should_retry = should_retry or (RUNNING and DISCONNECT_REQUESTED.is_set())

        except Exception as exc:
            log_event("connect_error", error=str(exc))
            should_retry = RUNNING

        finally:
            try:
                if interface is not None:
                    interface.close()
            except Exception:
                pass
            clear_packet_activity()
            DISCONNECT_REQUESTED.clear()

        if should_retry:
            log_event("reconnect_scheduled", delay_sec=reconnect_delay_sec)
            time.sleep(reconnect_delay_sec)

    log_event("service_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
