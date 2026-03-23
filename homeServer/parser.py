from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedEvent:
    packet_type: str
    reason: str | None
    source_node_id: str | None
    source_name: str | None
    msg_id: int | None
    received_at_utc: str
    source_ts_utc: str | None
    raw_payload: str
    normalized: dict[str, Any] | None


def _as_int(value: Any) -> int:
    return int(value)


def _as_float(value: Any) -> float:
    return float(value)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def payload_hash(raw_payload: str) -> str:
    return hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()


def parse_text_payload(
    *,
    text: str,
    source_node_id: str | None,
    source_name: str | None,
    received_at_utc: str,
) -> ParsedEvent:
    raw_payload = text

    try:
        obj = json.loads(text)
    except Exception:
        return ParsedEvent(
            packet_type="invalid",
            reason="malformed_json",
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=None,
            received_at_utc=received_at_utc,
            source_ts_utc=None,
            raw_payload=raw_payload,
            normalized=None,
        )

    if not isinstance(obj, dict):
        return ParsedEvent(
            packet_type="invalid",
            reason="json_not_object",
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=None,
            received_at_utc=received_at_utc,
            source_ts_utc=None,
            raw_payload=raw_payload,
            normalized=None,
        )

    event_type = obj.get("et")
    if isinstance(event_type, str):
        if event_type == "obs_st":
            return _parse_obs_st(
                obj=obj,
                raw_payload=raw_payload,
                source_node_id=source_node_id,
                source_name=source_name,
                received_at_utc=received_at_utc,
            )
        if event_type in ("evt_precip", "evt_strike"):
            return _parse_weather_event(
                obj=obj,
                raw_payload=raw_payload,
                source_node_id=source_node_id,
                source_name=source_name,
                received_at_utc=received_at_utc,
            )
        if event_type in ("device_status", "hub_status"):
            return _parse_device_telemetry(
                obj=obj,
                raw_payload=raw_payload,
                source_node_id=source_node_id,
                source_name=source_name,
                received_at_utc=received_at_utc,
            )

    # Legacy Pico heartbeat/debug payloads
    if "sys" in obj:
        try:
            msg_id = _as_int(obj.get("i")) if obj.get("i") is not None else None
            normalized = {
                "status": str(obj["sys"]),
                "msg_id": msg_id,
                "uptime_sec": _as_int(obj["up"]) if obj.get("up") is not None else None,
                "ip_address": _as_text(obj.get("ip")),
                "error_reason": _as_text(obj.get("err")),
                "source_ts_utc": _as_text(obj.get("ts")),
            }
            return ParsedEvent(
                packet_type="health",
                reason=None,
                source_node_id=source_node_id,
                source_name=source_name,
                msg_id=msg_id,
                received_at_utc=received_at_utc,
                source_ts_utc=normalized["source_ts_utc"],
                raw_payload=raw_payload,
                normalized=normalized,
            )
        except Exception as exc:
            return ParsedEvent(
                packet_type="invalid",
                reason=f"bad_health_payload:{exc}",
                source_node_id=source_node_id,
                source_name=source_name,
                msg_id=None,
                received_at_utc=received_at_utc,
                source_ts_utc=None,
                raw_payload=raw_payload,
                normalized=None,
            )

    # Legacy compact weather payloads from older bridge versions.
    weather_keys = {"i", "t", "h", "p", "w", "d", "r"}
    if weather_keys.issubset(obj.keys()):
        return _parse_legacy_weather(
            obj=obj,
            raw_payload=raw_payload,
            source_node_id=source_node_id,
            source_name=source_name,
            received_at_utc=received_at_utc,
        )

    return ParsedEvent(
        packet_type="unknown",
        reason="unrecognized_payload_shape",
        source_node_id=source_node_id,
        source_name=source_name,
        msg_id=obj.get("i") if isinstance(obj.get("i"), int) else None,
        received_at_utc=received_at_utc,
        source_ts_utc=_as_text(obj.get("ts")),
        raw_payload=raw_payload,
        normalized=obj,
    )


def _parse_obs_st(
    *,
    obj: dict[str, Any],
    raw_payload: str,
    source_node_id: str | None,
    source_name: str | None,
    received_at_utc: str,
) -> ParsedEvent:
    try:
        normalized = {
            "event_type": "obs_st",
            "msg_id": _as_int(obj["i"]),
            "source_ts_utc": _as_text(obj.get("ts")),
            "weather_timestamp": _as_int(obj["ts"]) if obj.get("ts") is not None else None,
            "temp_c": _as_float(obj["t"]),
            "humidity_pct": _as_float(obj["h"]),
            "pressure_hpa": _as_float(obj["p"]),
            "wind_ms": _as_float(obj["w"]),
            "wind_dir_deg": _as_int(obj["d"]),
            "rain_mm": _as_float(obj["r"]),
            "wind_lull_ms": _as_float(obj["l"]) if obj.get("l") is not None else None,
            "wind_gust_ms": _as_float(obj["g"]) if obj.get("g") is not None else None,
            "wind_sample_interval_s": _as_int(obj["ws"]) if obj.get("ws") is not None else None,
            "illuminance_lux": _as_float(obj["lux"]) if obj.get("lux") is not None else None,
            "uv_index": _as_float(obj["uv"]) if obj.get("uv") is not None else None,
            "solar_radiation_wm2": _as_float(obj["sr"]) if obj.get("sr") is not None else None,
            "precipitation_type": _as_int(obj["pt"]) if obj.get("pt") is not None else None,
            "lightning_avg_distance_km": _as_float(obj["ld"]) if obj.get("ld") is not None else None,
            "lightning_strike_count": _as_int(obj["lc"]) if obj.get("lc") is not None else None,
            "battery_voltage_v": _as_float(obj["bat"]) if obj.get("bat") is not None else None,
            "report_interval_min": _as_int(obj["ri"]) if obj.get("ri") is not None else None,
            "local_day_rain_mm": _as_float(obj["rd"]) if obj.get("rd") is not None else None,
            "nearcast_rain_mm": _as_float(obj["nr"]) if obj.get("nr") is not None else None,
            "local_day_nearcast_rain_mm": _as_float(obj["nrd"]) if obj.get("nrd") is not None else None,
            "precipitation_analysis_type": _as_int(obj["pa"]) if obj.get("pa") is not None else None,
        }

        _validate_weather(normalized)

        return ParsedEvent(
            packet_type="weather",
            reason=None,
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=normalized["msg_id"],
            received_at_utc=received_at_utc,
            source_ts_utc=normalized["source_ts_utc"],
            raw_payload=raw_payload,
            normalized=normalized,
        )
    except Exception as exc:
        return ParsedEvent(
            packet_type="rejected",
            reason=str(exc),
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=obj.get("i") if isinstance(obj.get("i"), int) else None,
            received_at_utc=received_at_utc,
            source_ts_utc=_as_text(obj.get("ts")),
            raw_payload=raw_payload,
            normalized=None,
        )


def _parse_legacy_weather(
    *,
    obj: dict[str, Any],
    raw_payload: str,
    source_node_id: str | None,
    source_name: str | None,
    received_at_utc: str,
) -> ParsedEvent:
    try:
        normalized = {
            "event_type": "obs_st",
            "msg_id": _as_int(obj["i"]),
            "source_ts_utc": _as_text(obj.get("ts")),
            "weather_timestamp": _as_int(obj["ts"]) if obj.get("ts") is not None else None,
            "temp_c": _as_float(obj["t"]),
            "humidity_pct": _as_float(obj["h"]),
            "pressure_hpa": _as_float(obj["p"]),
            "wind_ms": _as_float(obj["w"]),
            "wind_dir_deg": _as_int(obj["d"]),
            "rain_mm": _as_float(obj["r"]),
            "wind_lull_ms": None,
            "wind_gust_ms": _as_float(obj["g"]) if obj.get("g") is not None else None,
            "wind_sample_interval_s": _as_int(obj["ws"]) if obj.get("ws") is not None else None,
            "illuminance_lux": _as_float(obj["lux"]) if obj.get("lux") is not None else None,
            "uv_index": _as_float(obj["uv"]) if obj.get("uv") is not None else None,
            "solar_radiation_wm2": _as_float(obj["sr"]) if obj.get("sr") is not None else None,
            "precipitation_type": _as_int(obj["pt"]) if obj.get("pt") is not None else None,
            "lightning_avg_distance_km": _as_float(obj["ld"]) if obj.get("ld") is not None else None,
            "lightning_strike_count": _as_int(obj["lc"]) if obj.get("lc") is not None else None,
            "battery_voltage_v": _as_float(obj["bat"]) if obj.get("bat") is not None else None,
            "report_interval_min": _as_int(obj["ri"]) if obj.get("ri") is not None else None,
            "local_day_rain_mm": _as_float(obj["rd"]) if obj.get("rd") is not None else None,
            "nearcast_rain_mm": _as_float(obj["nr"]) if obj.get("nr") is not None else None,
            "local_day_nearcast_rain_mm": _as_float(obj["nrd"]) if obj.get("nrd") is not None else None,
            "precipitation_analysis_type": _as_int(obj["pa"]) if obj.get("pa") is not None else None,
        }

        _validate_weather(normalized)

        return ParsedEvent(
            packet_type="weather",
            reason=None,
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=normalized["msg_id"],
            received_at_utc=received_at_utc,
            source_ts_utc=normalized["source_ts_utc"],
            raw_payload=raw_payload,
            normalized=normalized,
        )
    except Exception as exc:
        return ParsedEvent(
            packet_type="rejected",
            reason=str(exc),
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=obj.get("i") if isinstance(obj.get("i"), int) else None,
            received_at_utc=received_at_utc,
            source_ts_utc=_as_text(obj.get("ts")),
            raw_payload=raw_payload,
            normalized=None,
        )


def _parse_weather_event(
    *,
    obj: dict[str, Any],
    raw_payload: str,
    source_node_id: str | None,
    source_name: str | None,
    received_at_utc: str,
) -> ParsedEvent:
    try:
        event_type = str(obj["et"])
        normalized = {
            "event_type": event_type,
            "msg_id": _as_int(obj["i"]),
            "source_ts_utc": _as_text(obj.get("ts")),
            "event_timestamp": _as_int(obj["ts"]) if obj.get("ts") is not None else None,
            "lightning_distance_km": _as_float(obj["ld"]) if obj.get("ld") is not None else None,
            "lightning_energy": _as_int(obj["se"]) if obj.get("se") is not None else None,
        }
        _validate_weather_event(normalized)
        return ParsedEvent(
            packet_type="weather_event",
            reason=None,
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=normalized["msg_id"],
            received_at_utc=received_at_utc,
            source_ts_utc=normalized["source_ts_utc"],
            raw_payload=raw_payload,
            normalized=normalized,
        )
    except Exception as exc:
        return ParsedEvent(
            packet_type="rejected",
            reason=str(exc),
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=obj.get("i") if isinstance(obj.get("i"), int) else None,
            received_at_utc=received_at_utc,
            source_ts_utc=_as_text(obj.get("ts")),
            raw_payload=raw_payload,
            normalized=None,
        )


def _parse_device_telemetry(
    *,
    obj: dict[str, Any],
    raw_payload: str,
    source_node_id: str | None,
    source_name: str | None,
    received_at_utc: str,
) -> ParsedEvent:
    try:
        event_type = str(obj["et"])
        normalized = {
            "event_type": event_type,
            "msg_id": _as_int(obj["i"]),
            "source_ts_utc": _as_text(obj.get("ts")),
            "telemetry_timestamp": _as_int(obj["ts"]) if obj.get("ts") is not None else None,
            "uptime_sec": _as_int(obj["up"]) if obj.get("up") is not None else None,
            "firmware_revision": _as_text(obj.get("fw")),
            "rssi": _as_int(obj["r"]) if obj.get("r") is not None else None,
            "hub_rssi": _as_int(obj["hr"]) if obj.get("hr") is not None else None,
            "sensor_status": _as_int(obj["ss"]) if obj.get("ss") is not None else None,
            "debug": _as_int(obj["dbg"]) if obj.get("dbg") is not None else None,
            "voltage": _as_float(obj["v"]) if obj.get("v") is not None else None,
            "reset_flags": _as_text(obj.get("rf")),
            "seq": _as_int(obj["seq"]) if obj.get("seq") is not None else None,
            "fs": obj.get("fs"),
            "radio_stats": obj.get("rs"),
            "mqtt_stats": obj.get("ms"),
        }
        _validate_telemetry(normalized)
        return ParsedEvent(
            packet_type="telemetry",
            reason=None,
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=normalized["msg_id"],
            received_at_utc=received_at_utc,
            source_ts_utc=normalized["source_ts_utc"],
            raw_payload=raw_payload,
            normalized=normalized,
        )
    except Exception as exc:
        return ParsedEvent(
            packet_type="rejected",
            reason=str(exc),
            source_node_id=source_node_id,
            source_name=source_name,
            msg_id=obj.get("i") if isinstance(obj.get("i"), int) else None,
            received_at_utc=received_at_utc,
            source_ts_utc=_as_text(obj.get("ts")),
            raw_payload=raw_payload,
            normalized=None,
        )


def _validate_weather(n: dict[str, Any]) -> None:
    if n["source_ts_utc"] is None:
        raise ValueError("weather_missing_ts")
    if not (-60 <= n["temp_c"] <= 70):
        raise ValueError("temp_out_of_range")
    if not (0 <= n["humidity_pct"] <= 100):
        raise ValueError("humidity_out_of_range")
    if not (800 <= n["pressure_hpa"] <= 1200):
        raise ValueError("pressure_out_of_range")
    if not (0 <= n["wind_ms"] <= 100):
        raise ValueError("wind_out_of_range")
    if not (0 <= n["wind_dir_deg"] <= 360):
        raise ValueError("wind_dir_out_of_range")
    if not (0 <= n["rain_mm"] <= 500):
        raise ValueError("rain_out_of_range")
    if n["wind_lull_ms"] is not None and not (0 <= n["wind_lull_ms"] <= 100):
        raise ValueError("wind_lull_out_of_range")
    if n["wind_gust_ms"] is not None and not (0 <= n["wind_gust_ms"] <= 120):
        raise ValueError("wind_gust_out_of_range")
    if n["wind_sample_interval_s"] is not None and not (0 <= n["wind_sample_interval_s"] <= 120):
        raise ValueError("wind_sample_interval_out_of_range")
    if n["illuminance_lux"] is not None and not (0 <= n["illuminance_lux"] <= 200000):
        raise ValueError("illuminance_out_of_range")
    if n["uv_index"] is not None and not (0 <= n["uv_index"] <= 30):
        raise ValueError("uv_out_of_range")
    if n["solar_radiation_wm2"] is not None and not (0 <= n["solar_radiation_wm2"] <= 2000):
        raise ValueError("solar_radiation_out_of_range")
    if n["precipitation_type"] is not None and not (0 <= n["precipitation_type"] <= 4):
        raise ValueError("precipitation_type_out_of_range")
    if n["lightning_avg_distance_km"] is not None and not (0 <= n["lightning_avg_distance_km"] <= 500):
        raise ValueError("lightning_distance_out_of_range")
    if n["lightning_strike_count"] is not None and not (0 <= n["lightning_strike_count"] <= 10000):
        raise ValueError("lightning_count_out_of_range")
    if n["battery_voltage_v"] is not None and not (0 <= n["battery_voltage_v"] <= 10):
        raise ValueError("battery_voltage_out_of_range")
    if n["report_interval_min"] is not None and not (0 <= n["report_interval_min"] <= 120):
        raise ValueError("report_interval_out_of_range")
    if n["local_day_rain_mm"] is not None and not (0 <= n["local_day_rain_mm"] <= 2000):
        raise ValueError("local_day_rain_out_of_range")
    if n["nearcast_rain_mm"] is not None and not (0 <= n["nearcast_rain_mm"] <= 2000):
        raise ValueError("nearcast_rain_out_of_range")
    if n["local_day_nearcast_rain_mm"] is not None and not (0 <= n["local_day_nearcast_rain_mm"] <= 2000):
        raise ValueError("local_day_nearcast_rain_out_of_range")
    if n["precipitation_analysis_type"] is not None and not (0 <= n["precipitation_analysis_type"] <= 10):
        raise ValueError("precipitation_analysis_type_out_of_range")


def _validate_weather_event(n: dict[str, Any]) -> None:
    if n["source_ts_utc"] is None:
        raise ValueError("event_missing_ts")
    if n["event_type"] == "evt_precip":
        return
    if n["event_type"] == "evt_strike":
        if n["lightning_distance_km"] is None:
            raise ValueError("strike_missing_distance")
        if n["lightning_energy"] is None:
            raise ValueError("strike_missing_energy")
        if not (0 <= n["lightning_distance_km"] <= 500):
            raise ValueError("strike_distance_out_of_range")
        if not (0 <= n["lightning_energy"] <= 1000000000):
            raise ValueError("strike_energy_out_of_range")
        return
    raise ValueError("unsupported_weather_event")


def _validate_telemetry(n: dict[str, Any]) -> None:
    if n["source_ts_utc"] is None:
        raise ValueError("telemetry_missing_ts")
    if n["event_type"] == "device_status":
        if n["uptime_sec"] is not None and n["uptime_sec"] < 0:
            raise ValueError("uptime_out_of_range")
        if n["voltage"] is not None and not (0 <= n["voltage"] <= 10):
            raise ValueError("voltage_out_of_range")
        if n["rssi"] is not None and not (-200 <= n["rssi"] <= 50):
            raise ValueError("rssi_out_of_range")
        if n["hub_rssi"] is not None and not (-200 <= n["hub_rssi"] <= 50):
            raise ValueError("hub_rssi_out_of_range")
        if n["sensor_status"] is not None and n["sensor_status"] < 0:
            raise ValueError("sensor_status_out_of_range")
        if n["debug"] is not None and n["debug"] not in (0, 1):
            raise ValueError("debug_out_of_range")
        return
    if n["event_type"] == "hub_status":
        if n["uptime_sec"] is not None and n["uptime_sec"] < 0:
            raise ValueError("uptime_out_of_range")
        if n["rssi"] is not None and not (-200 <= n["rssi"] <= 50):
            raise ValueError("rssi_out_of_range")
        if n["seq"] is not None and n["seq"] < 0:
            raise ValueError("seq_out_of_range")
        if n["fs"] is not None and not isinstance(n["fs"], list):
            raise ValueError("fs_not_list")
        if n["radio_stats"] is not None and not isinstance(n["radio_stats"], list):
            raise ValueError("radio_stats_not_list")
        if n["mqtt_stats"] is not None and not isinstance(n["mqtt_stats"], list):
            raise ValueError("mqtt_stats_not_list")
        return
    raise ValueError("unsupported_telemetry_type")
