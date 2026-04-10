#!/usr/bin/env python3
"""
Listen for Tempest-style UDP packets and validate supported payload shapes.

This test utility accepts the Tempest packet types used by the project:
`obs_st`, `rapid_wind`, `evt_precip`, `evt_strike`, `device_status`, and `hub_status`.
Each received packet is parsed as JSON, validated for the expected structure,
and logged as either valid or invalid with enough detail to troubleshoot the
payload quickly.

Use for validation that actual weather station is generating expected packets. If invalid packets recieved,
then we need to update the listener code to handle the new packet structure. 
Also useful for testing the listener code with a mock sender that can generate valid and invalid packets on demand.

"""

import argparse
import json
import logging
import socket
from typing import Any, Callable, Dict, Optional, Tuple

OBS_ST_FIELD_COUNT = 18


def missing_required_keys(message: Dict[str, Any], required_keys: Tuple[str, ...]) -> Optional[str]:
    missing = [key for key in required_keys if key not in message]
    if missing:
        return "missing required key(s): " + ", ".join(missing)
    return None


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_obs_st(message: Dict[str, Any]) -> Optional[str]:
    error = missing_required_keys(
        message,
        ("serial_number", "hub_sn", "obs", "firmware_revision"),
    )
    if error:
        return error

    obs = message["obs"]
    if not isinstance(obs, list) or not obs:
        return "obs must be a non-empty list"

    for index, entry in enumerate(obs):
        if not isinstance(entry, list):
            return f"obs[{index}] must be a list"
        if len(entry) != OBS_ST_FIELD_COUNT:
            return f"obs[{index}] expected {OBS_ST_FIELD_COUNT} values, got {len(entry)}"
        if not is_number(entry[0]):
            return f"obs[{index}][0] must be a numeric timestamp"

    return None


def validate_rapid_wind(message: Dict[str, Any]) -> Optional[str]:
    error = missing_required_keys(message, ("serial_number", "hub_sn", "ob"))
    if error:
        return error

    ob = message["ob"]
    if not isinstance(ob, list) or len(ob) < 3:
        return "ob must be a list with at least 3 values"
    if not is_number(ob[0]):
        return "ob[0] must be a numeric timestamp"
    if not is_number(ob[1]):
        return "ob[1] must be a numeric wind speed"
    if not is_number(ob[2]):
        return "ob[2] must be a numeric wind direction"

    return None


def validate_evt_precip(message: Dict[str, Any]) -> Optional[str]:
    error = missing_required_keys(message, ("serial_number", "hub_sn", "evt"))
    if error:
        return error

    evt = message["evt"]
    if not isinstance(evt, list) or len(evt) < 1:
        return "evt must be a list with at least 1 value"
    if not is_number(evt[0]):
        return "evt[0] must be a numeric timestamp"

    return None


def validate_evt_strike(message: Dict[str, Any]) -> Optional[str]:
    error = missing_required_keys(message, ("serial_number", "hub_sn", "evt"))
    if error:
        return error

    evt = message["evt"]
    if not isinstance(evt, list) or len(evt) < 3:
        return "evt must be a list with at least 3 values"
    if not is_number(evt[0]):
        return "evt[0] must be a numeric timestamp"
    if not is_number(evt[1]):
        return "evt[1] must be a numeric lightning distance"
    if not is_number(evt[2]):
        return "evt[2] must be a numeric strike energy"

    return None


def validate_device_status(message: Dict[str, Any]) -> Optional[str]:
    error = missing_required_keys(
        message,
        (
            "serial_number",
            "hub_sn",
            "timestamp",
            "uptime",
            "voltage",
            "firmware_revision",
            "rssi",
            "hub_rssi",
            "sensor_status",
            "debug",
        ),
    )
    if error:
        return error

    numeric_fields = (
        "timestamp",
        "uptime",
        "voltage",
        "rssi",
        "hub_rssi",
        "sensor_status",
        "debug",
    )
    for field in numeric_fields:
        if not is_number(message[field]):
            return f"{field} must be numeric"

    return None


def validate_hub_status(message: Dict[str, Any]) -> Optional[str]:
    error = missing_required_keys(
        message,
        (
            "serial_number",
            "timestamp",
            "uptime",
            "firmware_revision",
            "rssi",
            "reset_flags",
            "seq",
            "radio_stats",
            "mqtt_stats",
        ),
    )
    if error:
        return error

    numeric_fields = ("timestamp", "uptime", "rssi", "seq")
    for field in numeric_fields:
        if not is_number(message[field]):
            return f"{field} must be numeric"

    list_fields = ("radio_stats", "mqtt_stats")
    for field in list_fields:
        if not isinstance(message[field], list):
            return f"{field} must be a list"

    if "fs" in message and not isinstance(message["fs"], list):
        return "fs must be a list when present"

    return None


PACKET_VALIDATORS: Dict[str, Callable[[Dict[str, Any]], Optional[str]]] = {
    "obs_st": validate_obs_st,
    "rapid_wind": validate_rapid_wind,
    "evt_precip": validate_evt_precip,
    "evt_strike": validate_evt_strike,
    "device_status": validate_device_status,
    "hub_status": validate_hub_status,
}


def summarize_packet(message: Dict[str, Any]) -> str:
    packet_type = message["type"] 
    serial = message.get("serial_number", "unknown")
    hub = message.get("hub_sn")

    prefix = f"serial={serial}"
    if hub is not None:
        prefix += f", hub={hub}"

    if packet_type == "obs_st":
        return f"{prefix}, count={len(message['obs'])}"

    if packet_type == "rapid_wind":
        return (
            f"{prefix}, ts={int(message['ob'][0])}, "
            f"speed_mps={message['ob'][1]}, dir_deg={message['ob'][2]}"
        )

    if packet_type == "evt_precip":
        return f"{prefix}, ts={int(message['evt'][0])}"

    if packet_type == "evt_strike":
        return (
            f"{prefix}, ts={int(message['evt'][0])}, "
            f"distance_km={message['evt'][1]}, energy={message['evt'][2]}"
        )

    if packet_type == "device_status":
        return (
            f"{prefix}, ts={int(message['timestamp'])}, uptime={int(message['uptime'])}, "
            f"voltage={message['voltage']}"
        )

    if packet_type == "hub_status":
        return f"{prefix}, ts={int(message['timestamp'])}, seq={int(message['seq'])}"

    return prefix


def start_listener(port: int) -> None:
    """Start listening on the given UDP port and validate incoming messages."""
    logging.info("Listening for Tempest UDP packets on port %s...", port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(1.0)
    sock.bind(("", port))

    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            raw = data.decode("utf-8", errors="replace").strip()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logging.error("Malformed JSON from %s: %s", addr, raw)
                continue

            if not isinstance(message, dict):
                logging.error("Unexpected JSON root from %s: %s", addr, raw)
                continue

            packet_type = message.get("type")
            validator = PACKET_VALIDATORS.get(packet_type)
            if validator is None:
                logging.error("Unsupported packet type from %s: %s", addr, raw)
                continue

            error = validator(message)
            if error is None:
                logging.info("Valid %s from %s: %s", packet_type, addr, summarize_packet(message))
            else:
                logging.error("Invalid %s from %s: %s | payload=%s", packet_type, addr, error, raw)
    except KeyboardInterrupt:
        logging.info("Listener stopped by user")
    finally:
        sock.close()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Listen for WeatherFlow Tempest UDP packets and validate supported payloads"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=50222,
        help="UDP port to listen on (default: 50222)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    start_listener(args.port)


if __name__ == "__main__":
    main()
