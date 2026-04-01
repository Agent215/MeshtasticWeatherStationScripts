from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from .db import get_conn
    from .parser import ParsedEvent, payload_hash
except ImportError:
    from db import get_conn
    from parser import ParsedEvent, payload_hash


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_next_attempt(attempt_count: int) -> str:
    if attempt_count <= 1:
        delay = 30
    elif attempt_count == 2:
        delay = 120
    elif attempt_count == 3:
        delay = 600
    else:
        delay = 1800
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def _json_text(value: object | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def record_ingest_event(
    event: ParsedEvent,
    *,
    packet_type_override: str | None = None,
    reason_override: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ingest_events (
                received_at_utc, source_node_id, source_name,
                packet_type, reason, msg_id, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.received_at_utc,
                event.source_node_id,
                event.source_name,
                packet_type_override or event.packet_type,
                reason_override if reason_override is not None else event.reason,
                event.msg_id,
                event.raw_payload,
            ),
        )


def insert_weather(event: ParsedEvent) -> str:
    assert event.normalized is not None
    n = event.normalized

    source_ts_utc = n.get("source_ts_utc")
    if source_ts_utc is None or str(source_ts_utc).strip() == "":
        raise ValueError("weather_missing_source_ts_utc")

    source_ts_utc = str(source_ts_utc)

    with get_conn() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO weather_readings (
                    source_node_id, source_name, msg_id, source_ts_utc, received_at_utc,
                    temp_c, humidity_pct, pressure_hpa, wind_ms, wind_dir_deg, rain_mm,
                    wind_lull_ms, wind_gust_ms, wind_sample_interval_s,
                    illuminance_lux, uv_index, solar_radiation_wm2,
                    precipitation_type, lightning_avg_distance_km, lightning_strike_count,
                    battery_voltage_v, report_interval_min,
                    local_day_rain_mm, nearcast_rain_mm, local_day_nearcast_rain_mm,
                    precipitation_analysis_type, weather_timestamp,
                    raw_payload, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.source_node_id,
                    event.source_name,
                    n["msg_id"],
                    source_ts_utc,
                    event.received_at_utc,
                    n["temp_c"],
                    n["humidity_pct"],
                    n["pressure_hpa"],
                    n["wind_ms"],
                    n["wind_dir_deg"],
                    n["rain_mm"],
                    n.get("wind_lull_ms"),
                    n.get("wind_gust_ms"),
                    n.get("wind_sample_interval_s"),
                    n.get("illuminance_lux"),
                    n.get("uv_index"),
                    n.get("solar_radiation_wm2"),
                    n.get("precipitation_type"),
                    n.get("lightning_avg_distance_km"),
                    n.get("lightning_strike_count"),
                    n.get("battery_voltage_v"),
                    n.get("report_interval_min"),
                    n.get("local_day_rain_mm"),
                    n.get("nearcast_rain_mm"),
                    n.get("local_day_nearcast_rain_mm"),
                    n.get("precipitation_analysis_type"),
                    n.get("weather_timestamp"),
                    event.raw_payload,
                    payload_hash(event.raw_payload),
                ),
            )
        except sqlite3.IntegrityError:
            return "duplicate"

        reading_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO aws_delivery_queue (reading_id, status)
            VALUES (?, 'pending')
            """,
            (reading_id,),
        )

        conn.execute(
            """
            INSERT INTO device_status_current (
                source_node_id, source_name, last_weather_at_utc, last_msg_id,
                last_source_ts_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_node_id) DO UPDATE SET
                source_name=excluded.source_name,
                last_weather_at_utc=excluded.last_weather_at_utc,
                last_msg_id=excluded.last_msg_id,
                last_source_ts_utc=excluded.last_source_ts_utc,
                updated_at_utc=CURRENT_TIMESTAMP
            """,
            (
                event.source_node_id,
                event.source_name,
                event.received_at_utc,
                n["msg_id"],
                source_ts_utc,
            ),
        )

    return "inserted"


def insert_health(event: ParsedEvent) -> None:
    assert event.normalized is not None
    n = event.normalized

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO device_health_events (
                source_node_id, source_name, msg_id, source_ts_utc, received_at_utc,
                status, uptime_sec, ip_address, error_reason, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.source_node_id,
                event.source_name,
                n.get("msg_id"),
                n.get("source_ts_utc"),
                event.received_at_utc,
                n["status"],
                n.get("uptime_sec"),
                n.get("ip_address"),
                n.get("error_reason"),
                event.raw_payload,
            ),
        )

        conn.execute(
            """
            INSERT INTO device_status_current (
                source_node_id, source_name, last_health_at_utc, last_status,
                last_msg_id, last_source_ts_utc, last_uptime_sec, last_ip_address, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_node_id) DO UPDATE SET
                source_name=excluded.source_name,
                last_health_at_utc=excluded.last_health_at_utc,
                last_status=excluded.last_status,
                last_msg_id=excluded.last_msg_id,
                last_source_ts_utc=excluded.last_source_ts_utc,
                last_uptime_sec=excluded.last_uptime_sec,
                last_ip_address=excluded.last_ip_address,
                updated_at_utc=CURRENT_TIMESTAMP
            """,
            (
                event.source_node_id,
                event.source_name,
                event.received_at_utc,
                n["status"],
                n.get("msg_id"),
                n.get("source_ts_utc"),
                n.get("uptime_sec"),
                n.get("ip_address"),
            ),
        )


def insert_weather_event(event: ParsedEvent) -> None:
    assert event.normalized is not None
    n = event.normalized

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO weather_events (
                source_node_id, source_name, msg_id, event_type,
                source_ts_utc, received_at_utc,
                lightning_distance_km, lightning_energy,
                raw_payload, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.source_node_id,
                event.source_name,
                n.get("msg_id"),
                n["event_type"],
                n.get("source_ts_utc"),
                event.received_at_utc,
                n.get("lightning_distance_km"),
                n.get("lightning_energy"),
                event.raw_payload,
                payload_hash(event.raw_payload),
            ),
        )


def insert_device_telemetry(event: ParsedEvent) -> None:
    assert event.normalized is not None
    n = event.normalized

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO device_telemetry_events (
                source_node_id, source_name, msg_id, telemetry_type,
                source_ts_utc, received_at_utc,
                uptime_sec, firmware_revision, rssi, hub_rssi,
                sensor_status, debug, voltage,
                reset_flags, seq,
                fs_json, radio_stats_json, mqtt_stats_json,
                raw_payload, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.source_node_id,
                event.source_name,
                n.get("msg_id"),
                n["event_type"],
                n.get("source_ts_utc"),
                event.received_at_utc,
                n.get("uptime_sec"),
                n.get("firmware_revision"),
                n.get("rssi"),
                n.get("hub_rssi"),
                n.get("sensor_status"),
                n.get("debug"),
                n.get("voltage"),
                n.get("reset_flags"),
                n.get("seq"),
                _json_text(n.get("fs")),
                _json_text(n.get("radio_stats")),
                _json_text(n.get("mqtt_stats")),
                event.raw_payload,
                payload_hash(event.raw_payload),
            ),
        )

        if n["event_type"] == "device_status":
            conn.execute(
                """
                INSERT INTO device_status_current (
                    source_node_id, source_name,
                    last_device_status_at_utc, last_telemetry_type,
                    last_msg_id, last_source_ts_utc,
                    last_uptime_sec, last_firmware_revision,
                    last_rssi, last_hub_rssi,
                    last_sensor_status, last_debug,
                    last_device_voltage_v,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_node_id) DO UPDATE SET
                    source_name=excluded.source_name,
                    last_device_status_at_utc=excluded.last_device_status_at_utc,
                    last_telemetry_type=excluded.last_telemetry_type,
                    last_msg_id=excluded.last_msg_id,
                    last_source_ts_utc=excluded.last_source_ts_utc,
                    last_uptime_sec=excluded.last_uptime_sec,
                    last_firmware_revision=excluded.last_firmware_revision,
                    last_rssi=excluded.last_rssi,
                    last_hub_rssi=excluded.last_hub_rssi,
                    last_sensor_status=excluded.last_sensor_status,
                    last_debug=excluded.last_debug,
                    last_device_voltage_v=excluded.last_device_voltage_v,
                    updated_at_utc=CURRENT_TIMESTAMP
                """,
                (
                    event.source_node_id,
                    event.source_name,
                    event.received_at_utc,
                    n["event_type"],
                    n.get("msg_id"),
                    n.get("source_ts_utc"),
                    n.get("uptime_sec"),
                    n.get("firmware_revision"),
                    n.get("rssi"),
                    n.get("hub_rssi"),
                    n.get("sensor_status"),
                    n.get("debug"),
                    n.get("voltage"),
                ),
            )
        elif n["event_type"] == "hub_status":
            conn.execute(
                """
                INSERT INTO device_status_current (
                    source_node_id, source_name,
                    last_hub_status_at_utc, last_telemetry_type,
                    last_msg_id, last_source_ts_utc,
                    last_uptime_sec, last_firmware_revision,
                    last_rssi, last_reset_flags,
                    last_hub_seq, last_fs_json,
                    last_radio_stats_json, last_mqtt_stats_json,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_node_id) DO UPDATE SET
                    source_name=excluded.source_name,
                    last_hub_status_at_utc=excluded.last_hub_status_at_utc,
                    last_telemetry_type=excluded.last_telemetry_type,
                    last_msg_id=excluded.last_msg_id,
                    last_source_ts_utc=excluded.last_source_ts_utc,
                    last_uptime_sec=excluded.last_uptime_sec,
                    last_firmware_revision=excluded.last_firmware_revision,
                    last_rssi=excluded.last_rssi,
                    last_reset_flags=excluded.last_reset_flags,
                    last_hub_seq=excluded.last_hub_seq,
                    last_fs_json=excluded.last_fs_json,
                    last_radio_stats_json=excluded.last_radio_stats_json,
                    last_mqtt_stats_json=excluded.last_mqtt_stats_json,
                    updated_at_utc=CURRENT_TIMESTAMP
                """,
                (
                    event.source_node_id,
                    event.source_name,
                    event.received_at_utc,
                    n["event_type"],
                    n.get("msg_id"),
                    n.get("source_ts_utc"),
                    n.get("uptime_sec"),
                    n.get("firmware_revision"),
                    n.get("rssi"),
                    n.get("reset_flags"),
                    n.get("seq"),
                    _json_text(n.get("fs")),
                    _json_text(n.get("radio_stats")),
                    _json_text(n.get("mqtt_stats")),
                ),
            )


def fetch_pending_deliveries(limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                q.id AS queue_id,
                q.reading_id,
                q.status,
                q.attempt_count,
                q.next_attempt_at_utc,
                w.source_node_id,
                w.source_name,
                w.msg_id,
                w.source_ts_utc,
                w.received_at_utc,
                w.temp_c,
                w.humidity_pct,
                w.pressure_hpa,
                w.wind_ms,
                w.wind_dir_deg,
                w.rain_mm,
                w.wind_lull_ms,
                w.wind_gust_ms,
                w.wind_sample_interval_s,
                w.illuminance_lux,
                w.uv_index,
                w.solar_radiation_wm2,
                w.precipitation_type,
                w.lightning_avg_distance_km,
                w.lightning_strike_count,
                w.battery_voltage_v,
                w.report_interval_min,
                w.local_day_rain_mm,
                w.nearcast_rain_mm,
                w.local_day_nearcast_rain_mm,
                w.precipitation_analysis_type,
                w.weather_timestamp,
                w.raw_payload
            FROM aws_delivery_queue q
            JOIN weather_readings w ON w.id = q.reading_id
            WHERE q.status IN ('pending', 'retry')
              AND (
                    q.next_attempt_at_utc IS NULL
                    OR q.next_attempt_at_utc <= CURRENT_TIMESTAMP
                    OR q.next_attempt_at_utc <= ?
                  )
            ORDER BY q.id ASC
            LIMIT ?
            """,
            (utc_now(), limit),
        ).fetchall()
        return list(rows)


def mark_delivery_success(queue_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE aws_delivery_queue
            SET status = 'delivered',
                delivered_at_utc = CURRENT_TIMESTAMP,
                last_attempt_at_utc = CURRENT_TIMESTAMP,
                updated_at_utc = CURRENT_TIMESTAMP,
                last_error = NULL
            WHERE id = ?
            """,
            (queue_id,),
        )


def mark_delivery_failure(queue_id: int, error: str) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT attempt_count FROM aws_delivery_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if row is None:
            return

        new_attempt_count = int(row["attempt_count"]) + 1
        next_attempt = compute_next_attempt(new_attempt_count)

        conn.execute(
            """
            UPDATE aws_delivery_queue
            SET status = 'retry',
                attempt_count = ?,
                next_attempt_at_utc = ?,
                last_attempt_at_utc = CURRENT_TIMESTAMP,
                updated_at_utc = CURRENT_TIMESTAMP,
                last_error = ?
            WHERE id = ?
            """,
            (new_attempt_count, next_attempt, error[:500], queue_id),
        )
