#!/usr/bin/env python3
"""
Ecowitt LAN API mock server, revised against the official
HTTP API interface Protocol (Generic) V1.0.6 (2026-01-14).

Adds realistic randomized weather generation around configurable baseline
values so repeated GET /get_livedata_info calls look like a real station.
"""
from __future__ import annotations

import argparse
import json
import random
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def local_timestamp_pair() -> tuple[str, str]:
    now = datetime.now()
    return now.strftime("%Y-%m-%dT%H:%M:%S"), now.strftime("%m/%d/%Y %H:%M:%S")


def ok_status() -> dict[str, str]:
    return {"status": "0"}


DEFAULT_STATE: dict[str, Any] = {
    "network_info": {
        "mac": "E8:DB:84:0F:15:43",
        "ehtIpType": "0",
        "ethIP": "10.10.10.106",
        "ethMask": "255.255.255.0",
        "ethGateway": "10.10.10.100",
        "ssid": "GW1100A-WIFI38B4",
        "wifi_pwd": "",
        "wifi_ip": "192.168.4.10",
        "wifi_mask": "255.255.255.0",
        "wifi_gateway": "192.168.4.1",
    },
    "ws_settings": {
        "platform": "ecowitt",
        "ost_interval": "1",
        "sta_mac": "94:3C:C6:44:57:A7",
        "wu_interval": "1",
        "wu_id": "",
        "wu_key": "",
        "wcl_interval": "1",
        "wcl_id": "",
        "wcl_key": "",
        "wow_interval": "5",
        "wow_id": "",
        "wow_key": "",
        "Customized": "enable",
        "Protocol": "ecowitt",
        "ecowitt_ip": "tst.ecowitt.net",
        "ecowitt_path": "/data/report/",
        "ecowitt_port": "80",
        "ecowitt_upload": "60",
        "mqtt_name": "customized",
        "mqtt_host": "47.103.127.54",
        "mqtt_transport": "0",
        "mqtt_port": "1883",
        "mqtt_topic": "ecowitt/943CC64457A7",
        "mqtt_clientid": "gw2000-943CC64457A7",
        "mqtt_username": "system",
        "mqtt_password": "OstLsrling198mqtt",
        "mqtt_keepalive": "180",
        "mqtt_interval": "60",
        "usr_wu_path": "/weatherstation/updateweatherstation.php?",
        "usr_wu_id": "",
        "usr_wu_key": "",
        "usr_wu_port": "80",
        "usr_wu_upload": "60",
    },
    "scan_results": {
        "status": "0",
        "msg": "scan ok",
        "list": [
            {"ssid": "TP-LINK_DESK", "rssi": "3", "auth": "4"},
            {"ssid": "GW1100A-WIFI38B4", "rssi": "3", "auth": "0"},
            {"ssid": "garden-router", "rssi": "4", "auth": "4"},
        ],
    },
    "sensors_pages": {
        "1": [
            {"img": "wh90", "type": "48", "name": "Temp & Humidity & Solar & Wind & Rain", "id": "2B94", "batt": "4", "signal": "1", "idst": "1"},
            {"img": "wh25", "type": "4", "name": "Temp & Humidity & Pressure", "id": "FFFFFFFE", "batt": "9", "signal": "0", "idst": "0"},
            {"img": "wh57", "type": "26", "name": "Lightning", "id": "C497", "batt": "5", "signal": "3", "idst": "1"},
            {"img": "wh31", "type": "6", "name": "Temp & Humidity CH1", "id": "99", "batt": "0", "signal": "4", "idst": "1"},
        ],
        "2": [
            {"img": "wh51", "type": "19", "name": "Soil moisture CH6", "id": "C671", "batt": "5", "signal": "4", "idst": "1"},
            {"img": "wh34", "type": "31", "name": "Temp CH1", "id": "2C95", "batt": "5", "signal": "4", "idst": "1"},
            {"img": "wh54", "type": "66", "name": "Lds CH1", "id": "270D", "batt": "5", "signal": "4", "idst": "1"},
        ],
    },
    "rain_totals": {
        "rainFallPriority": "2",
        "list": [
            {"gauge": "No rain gauge", "value": "0"},
            {"gauge": "Traditional rain gauge", "value": "1"},
            {"gauge": "Piezoelectric rain gauge", "value": "2"},
        ],
        "rainDay": "0.0",
        "rainWeek": "0.0",
        "rainMonth": "0.0",
        "rainYear": "0.0",
        "rainGain": "1.00",
        "rstRainDay": "0",
        "rstRainWeek": "0",
        "rstRainYear": "0",
        "piezo": "1",
    },
    "piezo_rain": {
        "drain_piezo": "0.0",
        "wrain_piezo": "0.0",
        "mrain_piezo": "0.0",
        "yrain_piezo": "0.0",
        "rain1_gain": "1.00",
        "rain2_gain": "1.00",
        "rain3_gain": "1.00",
        "rain4_gain": "1.00",
        "rain5_gain": "1.00",
    },
    "calibration_data": {
        "SolarRadWave": "126.7",
        "solarRadGain": "1.00",
        "uvGain": "1.00",
        "windGain": "1.00",
        "inTempOffset": "0.0",
        "inHumiOffset": "0",
        "absOffset": "0.0",
        "relOffset": "0.0",
        "outTempOffset": "0.0",
        "outHumiOffset": "0",
        "windDirOffset": "0",
        "th_cli": True,
        "pm25_cli": True,
        "soil_cli": True,
        "co2_cli": True,
    },
    "cli_soilad": [
        {"id": "0x80C521", "ch": "1", "name": "", "soilVal": "0", "nowAd": "160", "minVal": "170", "maxVal": "320", "checked": False},
        {"id": "0x80C517", "ch": "2", "name": "", "soilVal": "0", "nowAd": "166", "minVal": "170", "maxVal": "320", "checked": False},
    ],
    "cli_multiCh": [
        {"id": "0x49", "name": "", "channel": "1", "temp": "0.0", "humi": "0"},
        {"id": "0x7A", "name": "", "channel": "2", "temp": "0.0", "humi": "0"},
    ],
    "cli_pm25": [
        {"id": "0xC4AD", "name": "", "channel": "1", "val": "1.0"},
        {"id": "0xA4", "name": "", "channel": "2", "val": "0.0"},
    ],
    "cli_co2": {"co2": "0", "pm25": "0.0", "pm10": "0.0"},
    "units_info": {"temperature": "1", "pressure": "0", "wind": "1", "rain": "0", "light": "1"},
    "device_info": {
        "sensorType": "1",
        "rf_freq": "2",
        "tz_auto": "0",
        "tz_name": "",
        "tz_index": "19",
        "dst_stat": "1",
        "date": "2026-03-17T18:56",
        "upgrade": "0",
        "apAuto": "1",
        "newVersion": "0",
        "curr_msg": "Current version:V2.1.4\r\n",
        "apName": "GW2000B-WIFI1543",
        "GW1100APpwd": "",
        "time": "20",
    },
    "version": {"version": "Version: GW2000B_V2.1.4", "newVersion": "1", "platform": "ecowitt"},
    "cli_lds": [
        {"id": "0x1234", "ch": "2", "name": "", "unit": "mm", "offset": "0", "total_height": "3999", "total_heat": "57702", "level": "4"},
        {"id": "0x29CC", "ch": "4", "name": "", "unit": "mm", "offset": "0", "total_height": "3999", "total_heat": "43", "level": "4"},
    ],
    "readings": {
        "out_temp_f": 79.2,
        "out_humidity_pct": 65,
        "feels_like_f": 79.2,
        "dewpoint_f": 66.4,
        "wind_speed_mph": 0.0,
        "gust_speed_mph": 1.12,
        "day_max_wind_mph": 1.34,
        "solar_wm2": 0.0,
        "uvi": 0,
        "vpd_kpa": 1.2,
        "bgt_c": 24.3,
        "wbgt_c": 22.6,
        "wind_dir_deg": 37,
        "rain_event_in": 0.0,
        "rain_rate_in_hr": 0.0,
        "rain_day_in": 0.0,
        "rain_week_in": 0.0,
        "rain_month_in": 0.0,
        "rain_year_in": 0.0,
        "indoor_temp_f": 81.0,
        "indoor_humidity_pct": 62,
        "baro_abs_inhg": 29.40,
        "baro_rel_inhg": 29.40,
        "lightning_distance_km": 34,
        "lightning_count": 0,
        "co2_temp_c": 24.0,
        "co2_humidity_pct": 50,
        "co2_ppm": 880,
        "co2_24h": 656,
        "pm25": 13.0,
        "pm25_real_aqi": 53,
        "pm25_24h_aqi": 60,
        "pm25_24h": 16.4,
        "pm10": 13.9,
        "pm10_real_aqi": 13,
        "pm10_24h_aqi": 18,
        "pm10_24h": 19.9,
        "pm1": 11.5,
        "pm1_real_aqi": 48,
        "pm1_24h_aqi": 52,
        "pm1_24h": 12.8,
        "pm4": 13.6,
        "pm4_real_aqi": 54,
        "pm4_24h_aqi": 65,
        "pm4_24h": 18.8,
        "ch_ec": [{"channel": "1", "name": "", "ec": "1.20", "unit": "ms/cm", "battery": "5"}],
    },
}


class AppState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.data = deepcopy(DEFAULT_STATE)
        self.random_seed = random.Random()
        self.last_wind_dir = int(self.data["readings"]["wind_dir_deg"])
        self.day_max_wind_mph = float(self.data["readings"]["day_max_wind_mph"])
        self.lightning_count = int(self.data["readings"]["lightning_count"])
        self.rain_day_in = float(self.data["readings"]["rain_day_in"])
        self.rain_week_in = float(self.data["readings"]["rain_week_in"])
        self.rain_month_in = float(self.data["readings"]["rain_month_in"])
        self.rain_year_in = float(self.data["readings"]["rain_year_in"])


STATE = AppState()


def ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise TypeError(f"Expected object, got {type(value).__name__}")


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    raise TypeError(f"Expected list, got {type(value).__name__}")


def parse_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    ctype = handler.headers.get("Content-Type", "")
    if "application/json" in ctype:
        return json.loads(raw)
    parsed = parse_qs(raw, keep_blank_values=True)
    if parsed:
        return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
    return {}


def merge(d: dict[str, Any], u: dict[str, Any]) -> None:
    for k, v in u.items():
        d[k] = v


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def chance(p: float) -> bool:
    return STATE.random_seed.random() < p


def fmt_num(value: float, decimals: int = 1) -> float:
    return round(value, decimals)


def generate_live_readings() -> dict[str, Any]:
    base = deepcopy(STATE.data["readings"])
    rng = STATE.random_seed

    out_temp_f = clamp(float(base["out_temp_f"]) + rng.uniform(-2.0, 2.0), 35.0, 100.0)
    out_hum = int(clamp(float(base["out_humidity_pct"]) + rng.uniform(-8, 8), 20, 100))
    indoor_temp_f = clamp(float(base["indoor_temp_f"]) + rng.uniform(-1.0, 1.0), 55.0, 95.0)
    indoor_hum = int(clamp(float(base["indoor_humidity_pct"]) + rng.uniform(-4, 4), 20, 85))

    wind_speed = clamp(float(base["wind_speed_mph"]) + rng.uniform(0, 3.5), 0.0, 18.0)
    if chance(0.25):
        wind_speed = clamp(wind_speed + rng.uniform(1.5, 5.0), 0.0, 22.0)
    gust_speed = clamp(max(wind_speed, wind_speed + rng.uniform(0.2, 4.5)), 0.0, 28.0)
    STATE.day_max_wind_mph = max(STATE.day_max_wind_mph, gust_speed)

    STATE.last_wind_dir = int((STATE.last_wind_dir + rng.randint(-20, 20)) % 360)

    solar = clamp(float(base["solar_wm2"]) + rng.uniform(-40, 150), 0.0, 1100.0)
    if solar < 25:
        solar = rng.uniform(0.0, 10.0)
    uvi = int(clamp(round(solar / 110.0 + rng.uniform(-0.5, 0.5)), 0, 11))

    baro_abs = clamp(float(base["baro_abs_inhg"]) + rng.uniform(-0.04, 0.04), 28.8, 30.5)
    baro_rel = clamp(float(base["baro_rel_inhg"]) + rng.uniform(-0.04, 0.04), 28.8, 30.5)

    dewpoint_f = clamp(out_temp_f - rng.uniform(6, 18), 20.0, out_temp_f)
    feels_like_f = clamp(out_temp_f + rng.uniform(-2.0, 3.0), 20.0, 115.0)
    vpd_kpa = clamp((100 - out_hum) / 40.0 + rng.uniform(-0.15, 0.15), 0.0, 4.0)
    bgt_c = clamp((out_temp_f - 32.0) * 5.0 / 9.0 + rng.uniform(-1.0, 1.0), -10.0, 50.0)
    wbgt_c = clamp(bgt_c - rng.uniform(0.5, 2.0), -10.0, 45.0)

    rain_event = 0.0
    rain_rate = 0.0
    if chance(0.12):
        rain_event = rng.uniform(0.01, 0.08)
        rain_rate = rng.uniform(0.02, 0.25)
        STATE.rain_day_in += rain_event
        STATE.rain_week_in += rain_event
        STATE.rain_month_in += rain_event
        STATE.rain_year_in += rain_event

    lightning_distance = int(clamp(float(base["lightning_distance_km"]) + rng.uniform(-6, 6), 1, 40))
    if chance(0.03):
        STATE.lightning_count += 1
        lightning_distance = int(rng.uniform(3, 15))

    co2_ppm = int(clamp(float(base["co2_ppm"]) + rng.uniform(-60, 80), 350, 2000))
    co2_24h = int(clamp(float(base["co2_24h"]) + rng.uniform(-20, 20), 350, 1800))
    co2_temp_c = clamp(float(base["co2_temp_c"]) + rng.uniform(-0.6, 0.6), 10.0, 40.0)
    co2_humidity = int(clamp(float(base["co2_humidity_pct"]) + rng.uniform(-4, 4), 20, 85))

    pm25 = clamp(float(base["pm25"]) + rng.uniform(-3, 4), 0.0, 150.0)
    pm10 = clamp(float(base["pm10"]) + rng.uniform(-3, 4), 0.0, 180.0)
    pm1 = clamp(float(base["pm1"]) + rng.uniform(-2, 3), 0.0, 120.0)
    pm4 = clamp(float(base["pm4"]) + rng.uniform(-2, 3), 0.0, 140.0)

    def aqiish(val: float) -> int:
        return int(clamp(round(val * 4.0 + rng.uniform(0, 8)), 0, 500))

    ec_entries = []
    for entry in base.get("ch_ec", []):
        e = dict(entry)
        ec_val = clamp(float(e.get("ec", 1.2)) + rng.uniform(-0.08, 0.08), 0.2, 3.5)
        e["ec"] = f"{ec_val:.2f}"
        ec_entries.append(e)

    live = {
        "out_temp_f": fmt_num(out_temp_f, 1),
        "out_humidity_pct": out_hum,
        "feels_like_f": fmt_num(feels_like_f, 1),
        "dewpoint_f": fmt_num(dewpoint_f, 1),
        "wind_speed_mph": fmt_num(wind_speed, 2),
        "gust_speed_mph": fmt_num(gust_speed, 2),
        "day_max_wind_mph": fmt_num(STATE.day_max_wind_mph, 2),
        "solar_wm2": fmt_num(solar, 2),
        "uvi": uvi,
        "vpd_kpa": fmt_num(vpd_kpa, 2),
        "bgt_c": fmt_num(bgt_c, 1),
        "wbgt_c": fmt_num(wbgt_c, 1),
        "wind_dir_deg": STATE.last_wind_dir,
        "rain_event_in": fmt_num(rain_event, 2),
        "rain_rate_in_hr": fmt_num(rain_rate, 2),
        "rain_day_in": fmt_num(STATE.rain_day_in, 2),
        "rain_week_in": fmt_num(STATE.rain_week_in, 2),
        "rain_month_in": fmt_num(STATE.rain_month_in, 2),
        "rain_year_in": fmt_num(STATE.rain_year_in, 2),
        "indoor_temp_f": fmt_num(indoor_temp_f, 1),
        "indoor_humidity_pct": indoor_hum,
        "baro_abs_inhg": fmt_num(baro_abs, 2),
        "baro_rel_inhg": fmt_num(baro_rel, 2),
        "lightning_distance_km": lightning_distance,
        "lightning_count": STATE.lightning_count,
        "co2_temp_c": fmt_num(co2_temp_c, 1),
        "co2_humidity_pct": co2_humidity,
        "co2_ppm": co2_ppm,
        "co2_24h": co2_24h,
        "pm25": fmt_num(pm25, 1),
        "pm25_real_aqi": aqiish(pm25),
        "pm25_24h_aqi": aqiish(pm25 + 1.5),
        "pm25_24h": fmt_num(pm25 + rng.uniform(0.5, 4.0), 1),
        "pm10": fmt_num(pm10, 1),
        "pm10_real_aqi": aqiish(pm10 / 2.5),
        "pm10_24h_aqi": aqiish(pm10 / 2.2),
        "pm10_24h": fmt_num(pm10 + rng.uniform(0.5, 5.0), 1),
        "pm1": fmt_num(pm1, 1),
        "pm1_real_aqi": aqiish(pm1),
        "pm1_24h_aqi": aqiish(pm1 + 1.0),
        "pm1_24h": fmt_num(pm1 + rng.uniform(0.2, 2.5), 1),
        "pm4": fmt_num(pm4, 1),
        "pm4_real_aqi": aqiish(pm4),
        "pm4_24h_aqi": aqiish(pm4 + 1.0),
        "pm4_24h": fmt_num(pm4 + rng.uniform(0.4, 3.5), 1),
        "ch_ec": ec_entries,
    }
    return live


def build_livedata_info() -> dict[str, Any]:
    r = generate_live_readings()
    iso_dt, ts_local = local_timestamp_pair()
    return {
        "common_list": [
            {"id": "0x02", "val": f"{r['out_temp_f']:.1f}", "unit": "F"},
            {"id": "0x07", "val": f"{int(r['out_humidity_pct'])}%"},
            {"id": "3", "val": f"{r['feels_like_f']:.1f}", "unit": "F"},
            {"id": "0x03", "val": f"{r['dewpoint_f']:.1f}", "unit": "F", "battery": "0"},
            {"id": "0x04", "val": f"{r['out_temp_f']:.1f}", "unit": "F"},
            {"id": "0x0B", "val": f"{r['wind_speed_mph']:.2f} mph"},
            {"id": "0x0C", "val": f"{r['gust_speed_mph']:.2f} mph"},
            {"id": "0x19", "val": f"{r['day_max_wind_mph']:.2f} mph"},
            {"id": "0x15", "val": f"{r['solar_wm2']:.2f} w/m2"},
            {"id": "0x17", "val": str(int(r['uvi']))},
            {"id": "0x0A", "val": str(int(r['wind_dir_deg'])), "battery": "5"},
            {"id": "0x05", "val": f"{r['vpd_kpa']:.2f}", "unit": "kPa"},
        ],
        "rain": [
            {"id": "0x0D", "val": f"{r['rain_event_in']:.2f} in"},
            {"id": "0x0E", "val": f"{r['rain_rate_in_hr']:.2f} in/Hr"},
            {"id": "0x10", "val": f"{r['rain_day_in']:.2f} in"},
            {"id": "0x11", "val": f"{r['rain_week_in']:.2f} in"},
            {"id": "0x12", "val": f"{r['rain_month_in']:.2f} in"},
            {"id": "0x13", "val": f"{r['rain_year_in']:.2f} in", "battery": "0"},
        ],
        "piezoRain": [
            {"id": "0x0D", "val": f"{r['rain_event_in']:.2f} in"},
            {"id": "0x0E", "val": f"{r['rain_rate_in_hr']:.2f} in/Hr"},
            {"id": "0x10", "val": f"{r['rain_day_in']:.2f} in"},
            {"id": "0x11", "val": f"{r['rain_week_in']:.2f} in"},
            {"id": "0x12", "val": f"{r['rain_month_in']:.2f} in"},
            {"id": "0x13", "val": f"{r['rain_year_in']:.2f} in", "battery": "4"},
        ],
        "wh25": [{
            "intemp": f"{r['indoor_temp_f']:.1f}",
            "unit": "F",
            "inhumi": f"{int(r['indoor_humidity_pct'])}%",
            "abs": f"{r['baro_abs_inhg']:.2f} inHg",
            "rel": f"{r['baro_rel_inhg']:.2f} inHg",
        }],
        "lightning": [{
            "distance": f"{int(r['lightning_distance_km'])} km",
            "date": iso_dt,
            "timestamp": ts_local,
            "count": str(int(r['lightning_count'])),
            "battery": "5",
        }],
        "co2": [{
            "temp": f"{r['co2_temp_c']:.1f}",
            "unit": "C",
            "humidity": f"{int(r['co2_humidity_pct'])}%",
            "PM25": f"{r['pm25']:.1f}",
            "PM25_RealAQI": str(int(r['pm25_real_aqi'])),
            "PM25_24HAQI": str(int(r['pm25_24h_aqi'])),
            "PM25_24H": f"{r['pm25_24h']:.1f}",
            "PM10": f"{r['pm10']:.1f}",
            "PM10_RealAQI": str(int(r['pm10_real_aqi'])),
            "PM10_24HAQI": str(int(r['pm10_24h_aqi'])),
            "PM10_24H": f"{r['pm10_24h']:.1f}",
            "PM1": f"{r['pm1']:.1f}",
            "PM1_RealAQI": str(int(r['pm1_real_aqi'])),
            "PM1_24HAQI": str(int(r['pm1_24h_aqi'])),
            "PM1_24H": f"{r['pm1_24h']:.1f}",
            "PM4": f"{r['pm4']:.1f}",
            "PM4_RealAQI": str(int(r['pm4_real_aqi'])),
            "PM4_24HAQI": str(int(r['pm4_24h_aqi'])),
            "PM4_24H": f"{r['pm4_24h']:.1f}",
            "CO2": str(int(r['co2_ppm'])),
            "CO2_24H": str(int(r['co2_24h'])),
            "battery": "6",
        }],
        "ch_pm25": [{"channel": "1", "PM25": f"{r['pm25']:.1f}", "PM25_RealAQI": str(int(r['pm25_real_aqi'])), "PM25_24HAQI": str(int(r['pm25_24h_aqi'])), "battery": "6"}],
        "ch_leak": [{"channel": "4", "name": "", "battery": "1", "status": "Normal"}],
        "ch_aisle": [{"channel": "1", "name": "", "battery": "0", "temp": "80.2", "unit": "F", "humidity": "None"}],
        "ch_soil": [{"channel": "1", "name": "", "battery": "1", "humidity": "0%"}],
        "ch_temp": [{"channel": "1", "name": "", "temp": "82.4", "unit": "F", "battery": "2"}],
        "ch_leaf": [{"channel": "1", "name": "", "humidity": "6%", "battery": "5"}],
        "ch_lds": [{"channel": "2", "name": "", "unit": "mm", "battery": "5", "voltage": "1.50", "air": "3044 mm", "depth": "955 mm"}],
        "ch_ec": r["ch_ec"],
        "debug": [{"heap": "91272", "runtime": "3323", "usr_interval": "60", "is_cnip": True}],
        "bgt": [{"val": f"{r['bgt_c']:.1f}", "unit": "C"}],
        "wbgt": [{"val": f"{r['wbgt_c']:.1f}", "unit": "C"}],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "EcowittMock/0.3"

    def sendj(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/get_livedata_info":
            self.sendj(build_livedata_info())
            return
        if route == "/get_network_info":
            self.sendj(STATE.data["network_info"])
            return
        if route == "/usr_scan_ssid_list":
            self.sendj(STATE.data["scan_results"])
            return
        if route == "/get_ws_settings":
            self.sendj(STATE.data["ws_settings"])
            return
        if route == "/get_sensors_info":
            page = query.get("page", ["1"])[0]
            self.sendj(STATE.data["sensors_pages"].get(page, []))
            return
        if route == "/get_rain_totals":
            self.sendj(STATE.data["rain_totals"])
            return
        if route == "/get_piezo_rain":
            self.sendj(STATE.data["piezo_rain"])
            return
        if route == "/get_calibration_data":
            self.sendj(STATE.data["calibration_data"])
            return
        if route == "/get_cli_soilad":
            self.sendj(STATE.data["cli_soilad"])
            return
        if route == "/get_cli_multiCh":
            self.sendj(STATE.data["cli_multiCh"])
            return
        if route == "/get_cli_pm25":
            self.sendj(STATE.data["cli_pm25"])
            return
        if route == "/get_cli_co2":
            self.sendj(STATE.data["cli_co2"])
            return
        if route == "/get_units_info":
            self.sendj(STATE.data["units_info"])
            return
        if route == "/get_device_info":
            self.sendj(STATE.data["device_info"])
            return
        if route == "/get_version":
            self.sendj(STATE.data["version"])
            return
        if route == "/get_cli_lds":
            self.sendj(STATE.data["cli_lds"])
            return
        if route == "/health":
            self.sendj({"ok": True})
            return
        self.sendj({"error": "not_found", "path": route}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        body = parse_body(self)

        if route == "/set_network_info":
            merge(STATE.data["network_info"], ensure_dict(body))
            self.sendj(ok_status())
            return
        if route == "/set_ws_settings":
            merge(STATE.data["ws_settings"], ensure_dict(body))
            self.sendj(ok_status())
            return
        if route == "/set_sensors_info":
            payload = ensure_dict(body)
            page = str(payload.pop("page", "1"))
            current = STATE.data["sensors_pages"].setdefault(page, [])
            current.append(payload)
            self.sendj({"status": "0"})
            return
        if route == "/set_rain_totals":
            merge(STATE.data["rain_totals"], ensure_dict(body))
            self.sendj(ok_status())
            return
        if route == "/set_piezo_rain":
            merge(STATE.data["piezo_rain"], ensure_dict(body))
            self.sendj(ok_status())
            return
        if route in {"/set_calibration_data", "/set_calibraion_data"}:
            merge(STATE.data["calibration_data"], ensure_dict(body))
            self.sendj(ok_status())
            return
        if route == "/set_cli_soilad":
            STATE.data["cli_soilad"] = ensure_list(body)
            self.sendj(ok_status())
            return
        if route == "/set_cli_multiCh":
            STATE.data["cli_multiCh"] = ensure_list(body)
            self.sendj(ok_status())
            return
        if route == "/set_cli_pm25":
            STATE.data["cli_pm25"] = ensure_list(body)
            self.sendj(ok_status())
            return
        if route == "/set_cli_co2":
            STATE.data["cli_co2"] = ensure_dict(body)
            self.sendj(ok_status())
            return
        if route == "/set_units_info":
            merge(STATE.data["units_info"], ensure_dict(body))
            self.sendj(ok_status())
            return
        if route == "/set_cli_lds":
            STATE.data["cli_lds"] = ensure_list(body)
            self.sendj(ok_status())
            return
        if route == "/upgrade_process":
            self.sendj({"is_new": False, "msg": "Current version:V2.1.4\\r\\n"})
            return
        if route == "/set_device_info":
            d = ensure_dict(body)
            if str(d.get("sysrestore", "0")) == "1":
                STATE.reset()
                self.sendj(ok_status())
                return
            if str(d.get("sysreboot", "0")) == "1":
                self.sendj(ok_status())
                return
            merge(STATE.data["device_info"], d)
            self.sendj(ok_status())
            return
        if route == "/__admin/set_readings":
            merge(STATE.data["readings"], ensure_dict(body))
            if "wind_dir_deg" in body:
                STATE.last_wind_dir = int(body["wind_dir_deg"])
            if "day_max_wind_mph" in body:
                STATE.day_max_wind_mph = float(body["day_max_wind_mph"])
            if "lightning_count" in body:
                STATE.lightning_count = int(body["lightning_count"])
            if "rain_day_in" in body:
                STATE.rain_day_in = float(body["rain_day_in"])
            if "rain_week_in" in body:
                STATE.rain_week_in = float(body["rain_week_in"])
            if "rain_month_in" in body:
                STATE.rain_month_in = float(body["rain_month_in"])
            if "rain_year_in" in body:
                STATE.rain_year_in = float(body["rain_year_in"])
            self.sendj({"ok": True, "readings": STATE.data["readings"]})
            return
        if route == "/__admin/reset":
            STATE.reset()
            self.sendj({"ok": True})
            return
        self.sendj({"error": "not_found", "path": route}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Ecowitt mock server listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
