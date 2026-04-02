#!/usr/bin/env python3
"""Standalone Meshtastic debug logger for the home-side USB radio.

This utility is for bring-up and troubleshooting only. It connects to the
Meshtastic device identified by `MESHTASTIC_DEVICE`, subscribes to packet,
text, and connection lifecycle events, and prints structured JSON to stdout.

Unlike `weatherstation/listen_meshtastic.py`, this script does not parse
weather payloads, write to SQLite, enqueue AWS delivery work, or participate
in the production ingest pipeline. Use it when you want to confirm serial
connectivity, inspect raw traffic, or debug reconnect behavior in isolation.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

from pubsub import pub
import meshtastic.serial_interface

RUNNING = True
DISCONNECT_REQUESTED = threading.Event()

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

def on_receive(packet: dict[str, Any], interface: Any) -> None:
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

def on_text(packet: dict[str, Any], interface: Any) -> None:
    from_id, from_name = get_source_info(packet)
    text = decode_payload(packet)
    log_event(
        "text_message_received",
        from_id=from_id,
        from_name=from_name,
        payload=text,
    )

def on_connection_established(interface: Any, topic: str = None) -> None:
    DISCONNECT_REQUESTED.clear()
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

    device = os.environ.get("MESHTASTIC_DEVICE")
    reconnect_delay_sec = 5

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_text, "meshtastic.receive.text")
    pub.subscribe(on_connection_established, "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    log_event("service_start", device=device)

    while RUNNING:
        interface = None
        should_retry = False
        try:
            DISCONNECT_REQUESTED.clear()
            log_event("connecting", device=device)
            interface = meshtastic.serial_interface.SerialInterface(devPath=device)
            log_event("connected", device=device)

            while RUNNING and not DISCONNECT_REQUESTED.wait(timeout=1):
                pass

            should_retry = RUNNING and DISCONNECT_REQUESTED.is_set()

        except Exception as exc:
            log_event("connect_error", error=str(exc))
            should_retry = RUNNING

        finally:
            try:
                if interface is not None:
                    interface.close()
            except Exception:
                pass
            DISCONNECT_REQUESTED.clear()

        if should_retry:
            time.sleep(reconnect_delay_sec)

    log_event("service_stop")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
