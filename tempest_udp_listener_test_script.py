#!/usr/bin/env python3
"""
tempest_udp_listener.py
------------------------

This script acts as a simple test harness for the WeatherFlow Tempest UDP
interface. It listens for JSON‑formatted UDP packets on the specified port
and validates that incoming observation messages (type ``obs_st``) conform
to the documented array layout for Tempest observations.  According to the
Tempest API's observation definitions, a Tempest observation record contains
22 values indexed from 0 to 21, representing metrics such as wind lull,
average and gust, direction, sample interval, pressure, air temperature,
humidity, illuminance, UV index, solar radiation, rain accumulation,
precipitation type, lightning metrics, battery voltage, reporting interval,
local day rain accumulation, nearcast rain accumulation, local day nearcast
rain accumulation, and a precipitation analysis type【807359736648359†L118-L122】.

If a received packet matches this format the script logs a success message.
If the packet cannot be parsed as JSON, has an unexpected ``type`` value,
lacks the ``obs`` array, or contains an observation with an unexpected
number of elements, the script logs an error and includes the malformed
payload for troubleshooting.

The default listening port is ``50222``, which is the port WeatherFlow hubs
broadcast to for local UDP data【980962502770717†L10-L16】.  Use the ``--port``
argument to change the port if necessary.
"""

import argparse
import json
import logging
import socket
from typing import Any, Dict, List


def validate_obs_st(message: Dict[str, Any]) -> bool:
    """Validate a single obs_st message structure.

    The message must contain an ``obs`` key whose value is a list of lists.
    Each observation sublist must have exactly 22 elements as defined in the
    Tempest observation format【807359736648359†L118-L122】.

    Parameters
    ----------
    message : dict
        Decoded JSON message from the Tempest hub.

    Returns
    -------
    bool
        True if the message conforms to the expected obs_st format, False
        otherwise.
    """

    # Check for required fields
    if not isinstance(message, dict):
        return False
    if message.get("type") != "obs_st":
        return False
    obs = message.get("obs")
    if not isinstance(obs, list) or len(obs) == 0:
        return False

    # Validate each observation entry
    for entry in obs:
        if not isinstance(entry, list) or len(entry) != 22:
            return False
    return True


def start_listener(port: int) -> None:
    """Start listening on the given UDP port and validate incoming messages.

    Parameters
    ----------
    port : int
        The UDP port to bind to.
    """
    logging.info(f"Listening for Tempest UDP packets on port {port}…")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))

    try:
        while True:
            data, addr = sock.recvfrom(65535)  # buffer size
            raw = data.decode("utf-8", errors="replace").strip()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logging.error(f"Malformed JSON from {addr}: {raw}")
                continue

            if validate_obs_st(message):
                serial = message.get("serial_number", "unknown")
                hub = message.get("hub_sn", "unknown")
                logging.info(
                    f"Valid obs_st from {addr}: serial={serial}, hub={hub}, count={len(message['obs'])}"
                )
            else:
                logging.error(f"Invalid obs_st message from {addr}: {raw}")
    except KeyboardInterrupt:
        logging.info("Listener stopped by user")
    finally:
        sock.close()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Listen for WeatherFlow Tempest UDP packets and validate obs_st payloads"
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