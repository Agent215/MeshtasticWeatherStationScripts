#!/usr/bin/env python3
"""
Send a realistic stream of mock Tempest UDP packets for local testing.

This simulator maintains evolving station and hub state, then emits the same
mix of messages a Tempest installation would normally produce: periodic
`obs_st`, `device_status`, and `hub_status` packets, plus `evt_precip` when
rain starts and `evt_strike` when a simulated lightning event occurs. Command
line flags control the target address, port, send intervals, loop tick, and
lightning frequency. Each loop iteration checks which message types are due,
updates the synthetic weather model for new observations, sends JSON over UDP,
and prints the payload so runs are easy to inspect.
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import time
from dataclasses import dataclass, field

UDP_PORT = 50222
DEFAULT_OBS_INTERVAL_SEC = 60
DEFAULT_DEVICE_STATUS_INTERVAL_SEC = 60
DEFAULT_HUB_STATUS_INTERVAL_SEC = 10
DEFAULT_TICK_SEC = 1.0


@dataclass
class SimulatorState:
    station_sn: str = "ST-TEST0001"
    hub_sn: str = "HB-TEST0001"
    firmware_revision: int = 171
    hub_firmware_revision: str = "171"

    start_ts: int = field(default_factory=lambda: int(time.time()))
    rain_today_mm: float = 0.0
    nearcast_rain_today_mm: float = 0.0
    is_raining: bool = False
    battery_v: float = 2.48
    hub_seq: int = 1
    last_strike_epoch: int = 0

    wind_dir_deg: int = 240
    wind_avg_ms: float = 2.5
    pressure_hpa: float = 1012.0
    temp_c: float = 22.2
    humidity_pct: float = 54.0
    illuminance_lux: float = 12000.0
    uv_index: float = 2.0
    solar_radiation_wm2: float = 350.0

    def device_uptime_sec(self) -> int:
        return max(1, int(time.time()) - self.start_ts)

    def hub_uptime_sec(self) -> int:
        return self.device_uptime_sec()

    def maybe_reset_daily_totals(self, now: int) -> None:
        start_day = time.localtime(self.start_ts).tm_yday
        now_day = time.localtime(now).tm_yday
        if now_day != start_day:
            self.rain_today_mm = 0.0
            self.nearcast_rain_today_mm = 0.0
            self.start_ts = now

    def evolve_weather(self, strike_chance: float) -> dict:
        now = int(time.time())
        self.maybe_reset_daily_totals(now)

        self.wind_avg_ms = clamp(self.wind_avg_ms + random.uniform(-0.35, 0.35), 0.0, 8.0)
        wind_lull = clamp(self.wind_avg_ms - random.uniform(0.0, 0.8), 0.0, 7.0)
        wind_gust = clamp(self.wind_avg_ms + random.uniform(0.2, 2.2), self.wind_avg_ms, 12.0)
        self.wind_dir_deg = int((self.wind_dir_deg + random.randint(-12, 12)) % 360)

        self.pressure_hpa = round(clamp(self.pressure_hpa + random.uniform(-0.25, 0.25), 1007.0, 1020.0), 2)
        self.temp_c = round(clamp(self.temp_c + random.uniform(-0.15, 0.15), 18.0, 31.0), 2)
        self.humidity_pct = round(clamp(self.humidity_pct + random.uniform(-1.0, 1.0), 35.0, 95.0), 2)
        self.illuminance_lux = round(clamp(self.illuminance_lux + random.uniform(-1200, 1200), 0.0, 95000.0), 2)
        self.uv_index = round(clamp(self.uv_index + random.uniform(-0.15, 0.15), 0.0, 11.0), 2)
        self.solar_radiation_wm2 = round(clamp(self.solar_radiation_wm2 + random.uniform(-30, 30), 0.0, 1200.0), 2)
        self.battery_v = round(clamp(self.battery_v + random.uniform(-0.005, 0.005), 2.35, 2.8), 3)

        # Simple weather regime switching so rain comes and goes naturally.
        if self.is_raining:
            if random.random() < 0.18:
                self.is_raining = False
        else:
            if random.random() < 0.08:
                self.is_raining = True

        rain_interval_mm = 0.0
        if self.is_raining:
            rain_interval_mm = round(random.choice([0.1, 0.2, 0.3, 0.4, 0.6, 0.8]), 2)

        precip_type = 1 if rain_interval_mm > 0 else 0

        nearcast_interval_mm = 0.0
        precip_analysis_type = 0
        if rain_interval_mm > 0 and random.random() < 0.55:
            nearcast_interval_mm = round(clamp(rain_interval_mm + random.uniform(-0.05, 0.12), 0.0, 2.0), 2)
            precip_analysis_type = random.choice([1, 2])

        self.rain_today_mm = round(self.rain_today_mm + rain_interval_mm, 2)
        self.nearcast_rain_today_mm = round(self.nearcast_rain_today_mm + nearcast_interval_mm, 2)

        strike_count = 0
        strike_distance_km = 0
        if random.random() < strike_chance:
            strike_count = random.choice([1, 1, 1, 2])
            strike_distance_km = random.choice([1, 4, 6, 8, 10, 12, 15, 18, 22, 27])
            self.last_strike_epoch = now

        return {
            "timestamp": now,
            "wind_lull": round(wind_lull, 2),
            "wind_avg": round(self.wind_avg_ms, 2),
            "wind_gust": round(wind_gust, 2),
            "wind_dir": self.wind_dir_deg,
            "wind_sample_interval": 3,
            "pressure_hpa": self.pressure_hpa,
            "temp_c": self.temp_c,
            "humidity_pct": self.humidity_pct,
            "illuminance_lux": self.illuminance_lux,
            "uv": self.uv_index,
            "solar_radiation": self.solar_radiation_wm2,
            "rain_interval_mm": rain_interval_mm,
            "precip_type": precip_type,
            "lightning_avg_distance_km": strike_distance_km,
            "lightning_strike_count": strike_count,
            "battery_v": self.battery_v,
            "report_interval_min": 1,
            "local_day_rain_mm": self.rain_today_mm,
            "nearcast_rain_mm": nearcast_interval_mm,
            "local_day_nearcast_rain_mm": self.nearcast_rain_today_mm,
            "precip_analysis_type": precip_analysis_type,
        }


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def build_obs_st(state: SimulatorState, wx: dict) -> dict:
    obs = [[
        wx["timestamp"],
        wx["wind_lull"],
        wx["wind_avg"],
        wx["wind_gust"],
        wx["wind_dir"],
        wx["wind_sample_interval"],
        wx["pressure_hpa"],
        wx["temp_c"],
        wx["humidity_pct"],
        wx["illuminance_lux"],
        wx["uv"],
        wx["solar_radiation"],
        wx["rain_interval_mm"],
        wx["precip_type"],
        wx["lightning_avg_distance_km"],
        wx["lightning_strike_count"],
        wx["battery_v"],
        wx["report_interval_min"],
        wx["local_day_rain_mm"],
        wx["nearcast_rain_mm"],
        wx["local_day_nearcast_rain_mm"],
        wx["precip_analysis_type"],
    ]]
    return {
        "serial_number": state.station_sn,
        "type": "obs_st",
        "hub_sn": state.hub_sn,
        "obs": obs,
        "firmware_revision": state.firmware_revision,
    }


def build_evt_precip(state: SimulatorState, wx: dict) -> dict:
    return {
        "serial_number": state.station_sn,
        "type": "evt_precip",
        "hub_sn": state.hub_sn,
        "evt": [wx["timestamp"]],
    }


def build_evt_strike(state: SimulatorState, wx: dict) -> dict:
    return {
        "serial_number": state.station_sn,
        "type": "evt_strike",
        "hub_sn": state.hub_sn,
        "evt": [
            wx["timestamp"],
            wx["lightning_avg_distance_km"],
            random.randint(200, 6000),
        ],
    }


def build_device_status(state: SimulatorState) -> dict:
    return {
        "serial_number": state.station_sn,
        "type": "device_status",
        "hub_sn": state.hub_sn,
        "timestamp": int(time.time()),
        "uptime": state.device_uptime_sec(),
        "voltage": state.battery_v,
        "firmware_revision": state.firmware_revision,
        "rssi": random.randint(-78, -48),
        "hub_rssi": random.randint(-82, -52),
        "sensor_status": 0,
        "debug": 0,
    }


def build_hub_status(state: SimulatorState) -> dict:
    state.hub_seq += 1
    return {
        "serial_number": state.hub_sn,
        "type": "hub_status",
        "firmware_revision": state.hub_firmware_revision,
        "uptime": state.hub_uptime_sec(),
        "rssi": random.randint(-62, -35),
        "timestamp": int(time.time()),
        "reset_flags": "PIN,SFT",
        "seq": state.hub_seq,
        "fs": [1, 0, 15675411, 524288],
        "radio_stats": [25, 1, 0, 3, random.randint(1000, 65000)],
        "mqtt_stats": [random.randint(0, 150), random.randint(0, 10)],
    }


def make_socket(broadcast: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def send_packet(sock: socket.socket, target: str, port: int, packet: dict) -> None:
    payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    sock.sendto(payload, (target, port))
    print(payload.decode("utf-8"), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Tempest UDP multi-message sender")
    parser.add_argument(
        "--target",
        default="255.255.255.255",
        help="Target IP. Use a Pico/server IP for unicast, or 255.255.255.255 for broadcast.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=UDP_PORT,
        help="UDP port, default 50222.",
    )
    parser.add_argument(
        "--tick",
        type=float,
        default=DEFAULT_TICK_SEC,
        help="Main loop tick in seconds. Smaller values improve interval accuracy.",
    )
    parser.add_argument(
        "--obs-interval",
        type=float,
        default=DEFAULT_OBS_INTERVAL_SEC,
        help="Seconds between obs_st packets. Default 60.",
    )
    parser.add_argument(
        "--device-status-interval",
        type=float,
        default=DEFAULT_DEVICE_STATUS_INTERVAL_SEC,
        help="Seconds between device_status packets. Default 60.",
    )
    parser.add_argument(
        "--hub-status-interval",
        type=float,
        default=DEFAULT_HUB_STATUS_INTERVAL_SEC,
        help="Seconds between hub_status packets. Default 10.",
    )
    parser.add_argument(
        "--strike-chance",
        type=float,
        default=0.06,
        help="Approximate per-observation chance of emitting evt_strike. Default 0.06.",
    )
    args = parser.parse_args()

    broadcast = args.target == "255.255.255.255"
    sock = make_socket(broadcast=broadcast)
    state = SimulatorState()

    last_obs_at = 0.0
    last_device_status_at = 0.0
    last_hub_status_at = 0.0
    previous_raining = False

    print(
        f"Sending mock Tempest UDP packets to {args.target}:{args.port} "
        f"(obs_st every {args.obs_interval}s, "
        f"device_status every {args.device_status_interval}s, "
        f"hub_status every {args.hub_status_interval}s)",
        flush=True,
    )

    try:
        while True:
            now_monotonic = time.monotonic()

            if now_monotonic - last_hub_status_at >= args.hub_status_interval:
                send_packet(sock, args.target, args.port, build_hub_status(state))
                last_hub_status_at = now_monotonic

            if now_monotonic - last_device_status_at >= args.device_status_interval:
                send_packet(sock, args.target, args.port, build_device_status(state))
                last_device_status_at = now_monotonic

            if now_monotonic - last_obs_at >= args.obs_interval:
                wx = state.evolve_weather(args.strike_chance)
                send_packet(sock, args.target, args.port, build_obs_st(state, wx))

                raining_now = wx["rain_interval_mm"] > 0
                if raining_now and not previous_raining:
                    send_packet(sock, args.target, args.port, build_evt_precip(state, wx))
                previous_raining = raining_now

                if wx["lightning_strike_count"] > 0:
                    for _ in range(wx["lightning_strike_count"]):
                        send_packet(sock, args.target, args.port, build_evt_strike(state, wx))

                last_obs_at = now_monotonic

            time.sleep(args.tick)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
