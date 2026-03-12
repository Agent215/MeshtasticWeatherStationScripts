import argparse
import json
import random
import socket
import time

UDP_PORT = 50222


def build_obs_st(msg_num: int) -> dict:
    now = int(time.time())

    # Generate realistic but slightly changing weather values
    wind_lull = round(random.uniform(0.3, 1.2), 2)
    wind_avg = round(random.uniform(2.0, 4.5), 2)
    wind_gust = round(max(wind_avg + random.uniform(0.5, 2.5), wind_avg), 2)
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

    obs = [
        [
            now,                        # 0 timestamp
            wind_lull,                  # 1 wind lull (m/s)
            wind_avg,                   # 2 wind avg (m/s)
            wind_gust,                  # 3 wind gust (m/s)
            wind_dir,                   # 4 wind direction (deg)
            3,                          # 5 wind sample interval (s)
            pressure_hpa,               # 6 station pressure (hPa)
            temp_c,                     # 7 air temperature (C)
            humidity,                   # 8 relative humidity (%)
            illuminance_lux,            # 9 illuminance (lux)
            uv,                         # 10 UV
            solar_radiation,            # 11 solar radiation (W/m^2)
            rain_interval_mm,           # 12 rain accumulation over interval (mm)
            precip_type,                # 13 precipitation type
            lightning_avg_distance_km,  # 14 lightning avg distance (km)
            lightning_strike_count,     # 15 lightning strike count
            battery_v,                  # 16 battery (V)
            report_interval_min,        # 17 report interval (min)
            local_day_rain_mm,          # 18 local day rain accumulation (mm)
            nearcast_rain_mm,           # 19 Nearcast rain accumulation (mm)
            local_day_nearcast_rain_mm, # 20 local day Nearcast rain (mm)
            precip_analysis_type        # 21 precipitation analysis type
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


def make_socket(broadcast: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Tempest UDP obs_st sender")
    parser.add_argument(
        "--target",
        default="255.255.255.255",
        help="Target IP. Use a Pico IP for unicast, or 255.255.255.255 for broadcast."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Seconds between packets."
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
    print(f"Sending mock Tempest obs_st packets to {args.target}:{args.port} every {args.interval}s")

    try:
        while True:
            packet = build_obs_st(msg_num)
            payload = json.dumps(packet, separators=(",", ":")).encode("utf-8")
            sock.sendto(payload, (args.target, args.port))
            print(payload.decode("utf-8"))
            msg_num += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()