import argparse
import json
import random
import socket
import time

UDP_PORT = 50222


def build_weather_snapshot() -> dict:
    now = int(time.time())

    wind_avg = round(random.uniform(2.0, 4.5), 2)
    wind_dir = random.choice([225, 230, 235, 240, 245, 250, 255, 260])
    pressure_hpa = round(random.uniform(1009.8, 1013.6), 2)
    temp_c = round(random.uniform(21.8, 23.4), 2)
    humidity = round(random.uniform(50.0, 58.0), 2)

    illuminance_lux = round(random.uniform(8000, 18000), 2)
    uv = round(random.uniform(1.0, 3.2), 2)
    solar_radiation = round(random.uniform(200, 550), 2)

    rain_interval_mm = round(random.choice([0.0, 0.0, 0.0, 0.0, 0.2]), 2)
    precip_type = 0
    lightning_avg_distance_km = 0
    lightning_strike_count = 0
    battery_v = round(random.uniform(2.38, 2.52), 3)
    report_interval_min = 1
    local_day_rain_mm = round(rain_interval_mm, 2)
    nearcast_rain_mm = 0.0
    local_day_nearcast_rain_mm = 0.0
    precip_analysis_type = 0

    return {
        "timestamp": now,
        "wind_avg": wind_avg,
        "wind_dir": wind_dir,
        "pressure_hpa": pressure_hpa,
        "temp_c": temp_c,
        "humidity": humidity,
        "illuminance_lux": illuminance_lux,
        "uv": uv,
        "solar_radiation": solar_radiation,
        "rain_interval_mm": rain_interval_mm,
        "precip_type": precip_type,
        "lightning_avg_distance_km": lightning_avg_distance_km,
        "lightning_strike_count": lightning_strike_count,
        "battery_v": battery_v,
        "report_interval_min": report_interval_min,
    }


def build_obs_st(msg_num: int, wx: dict) -> dict:
    obs = [
        [
            wx["timestamp"],                # 0 timestamp
            None,                           # 1 wind lull omitted by live station
            None,                           # 2 wind avg omitted by live station
            None,                           # 3 wind gust omitted by live station
            None,                           # 4 wind direction omitted by live station
            60,                             # 5 wind sample interval / report cadence marker
            wx["pressure_hpa"],             # 6 station pressure (hPa)
            wx["temp_c"],                   # 7 air temperature (C)
            wx["humidity"],                 # 8 relative humidity (%)
            wx["illuminance_lux"],          # 9 illuminance (lux)
            wx["uv"],                       # 10 UV
            wx["solar_radiation"],          # 11 solar radiation (W/m^2)
            wx["rain_interval_mm"],         # 12 rain accumulation over interval (mm)
            wx["precip_type"],              # 13 precipitation type
            wx["lightning_avg_distance_km"],# 14 lightning avg distance (km)
            wx["lightning_strike_count"],   # 15 lightning strike count
            wx["battery_v"],                # 16 battery (V)
            wx["report_interval_min"],      # 17 report interval (min)
        ]
    ]

    return {
        "serial_number": "ST-TEST0001",
        "type": "obs_st",
        "hub_sn": "HB-TEST0001",
        "obs": obs,
        "firmware_revision": 171,
        "debug_msg_num": msg_num
    }


def build_rapid_wind(wx: dict) -> dict:
    return {
        "serial_number": "ST-TEST0001",
        "type": "rapid_wind",
        "hub_sn": "HB-TEST0001",
        "ob": [
            wx["timestamp"],
            wx["wind_avg"],
            wx["wind_dir"],
        ],
    }


def make_socket(broadcast: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Tempest UDP live-shape sender")
    parser.add_argument(
        "--target",
        default="255.255.255.255",
        help="Target IP. Use a Pico IP for unicast, or 255.255.255.255 for broadcast."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Seconds between obs_st packets."
    )
    parser.add_argument(
        "--rapid-wind-interval",
        type=float,
        default=3.0,
        help="Seconds between rapid_wind packets."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=UDP_PORT,
        help="UDP port, default 50222."
    )
    args = parser.parse_args()

    broadcast = args.target == "255.255.255.255"
    sock = make_socket(broadcast=broadcast)

    msg_num = 1
    print(
        f"Sending mock Tempest live-shape packets to {args.target}:{args.port} "
        f"(obs_st every {args.interval}s, rapid_wind every {args.rapid_wind_interval}s)"
    )

    try:
        last_obs_at = 0.0
        last_rapid_wind_at = 0.0
        while True:
            now_monotonic = time.monotonic()

            if now_monotonic - last_rapid_wind_at >= args.rapid_wind_interval:
                wx = build_weather_snapshot()
                packet = build_rapid_wind(wx)
                payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")
                sock.sendto(payload, (args.target, args.port))
                print(payload.decode("utf-8"))
                last_rapid_wind_at = now_monotonic

            if now_monotonic - last_obs_at >= args.interval:
                wx = build_weather_snapshot()
                packet = build_obs_st(msg_num, wx)
                payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")
                sock.sendto(payload, (args.target, args.port))
                print(payload.decode("utf-8"))
                msg_num += 1
                last_obs_at = now_monotonic

            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
