import os
import json
import base64
import re
from decimal import Decimal
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

ddb = boto3.client("dynamodb")
serializer = TypeSerializer()

TABLE_NAME = os.environ["TABLE_NAME"]
API_SHARED_SECRET = os.environ["API_SHARED_SECRET"]
ALLOWED_CORS_ORIGIN = os.environ.get("ALLOWED_CORS_ORIGIN", "*")

ISO_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)

WEATHER_RULES = {
    "wind_lull_ms": {"min": Decimal("0"), "max": Decimal("150")},
    "wind_avg_ms": {"min": Decimal("0"), "max": Decimal("150")},
    "wind_gust_ms": {"min": Decimal("0"), "max": Decimal("200")},
    "wind_dir_deg": {"min": Decimal("0"), "max": Decimal("360")},
    "wind_sample_interval_s": {"min": Decimal("0"), "max": Decimal("3600")},
    "station_pressure_hpa": {"min": Decimal("800"), "max": Decimal("1200")},
    "air_temp_c": {"min": Decimal("-100"), "max": Decimal("100")},
    "relative_humidity_pct": {"min": Decimal("0"), "max": Decimal("100")},
    "illuminance_lux": {"min": Decimal("0"), "max": Decimal("500000")},
    "uv_index": {"min": Decimal("0"), "max": Decimal("30")},
    "solar_radiation_wm2": {"min": Decimal("0"), "max": Decimal("2000")},
    "rain_interval_mm": {"min": Decimal("0"), "max": Decimal("1000")},
    "precipitation_type": {"allowed": [0, 1, 2, 3]},
    "lightning_avg_distance_km": {"min": Decimal("0"), "max": Decimal("1000")},
    "lightning_strike_count": {"min": Decimal("0"), "max": Decimal("1000000")},
    "battery_voltage_v": {"min": Decimal("0"), "max": Decimal("20")},
    "report_interval_min": {"min": Decimal("0"), "max": Decimal("1440")},
    "local_day_rain_mm": {"min": Decimal("0"), "max": Decimal("5000")},
    "nearcast_rain_mm": {"min": Decimal("0"), "max": Decimal("5000")},
    "local_day_nearcast_rain_mm": {"min": Decimal("0"), "max": Decimal("5000")},
    "precipitation_analysis_type": {"allowed": [0, 1, 2, 3, 4]},
}

def json_safe(value):
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    return value

def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": ALLOWED_CORS_ORIGIN,
            "access-control-allow-headers": "content-type,x-weatherstation-key",
            "access-control-allow-methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(json_safe(body)),
    }

def get_headers(event):
    return event.get("headers") or {}

def parse_body(event):
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body, parse_float=Decimal)

def auth_ok(event):
    headers = get_headers(event)
    provided = headers.get("x-weatherstation-key") or headers.get("X-Weatherstation-Key")
    return provided == API_SHARED_SECRET

def pad_msg_id(msg_id):
    return str(int(msg_id)).zfill(10)

def is_valid_iso_utc(ts):
    if not isinstance(ts, str):
        return False
    if not ISO_UTC_RE.match(ts):
        return False
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False

def validate_number_field(name, value):
    if not isinstance(value, (int, Decimal)):
        return f"{name} must be numeric"

    rules = WEATHER_RULES.get(name)
    if not rules:
        return None

    numeric_value = Decimal(str(value))

    if "allowed" in rules:
        allowed = rules["allowed"]
        if int(numeric_value) not in allowed:
            return f"{name} must be one of {allowed}"
        return None

    min_value = rules["min"]
    max_value = rules["max"]

    if numeric_value < min_value or numeric_value > max_value:
        return f"{name} must be between {min_value} and {max_value}"

    return None

def validate_weather(weather):
    if not isinstance(weather, dict):
        return ["payload.weather must be an object"]

    errors = []

    for key, value in weather.items():
        if key == "timestamp":
            if isinstance(value, str):
                if not is_valid_iso_utc(value):
                    errors.append("weather.timestamp must be ISO-8601 UTC like 2026-03-18T00:45:46.982324Z")
            elif isinstance(value, (int, Decimal)):
                if Decimal(str(value)) < 0:
                    errors.append("weather.timestamp epoch must be non-negative")
            else:
                errors.append("weather.timestamp must be ISO-8601 UTC string or epoch number")
            continue

        if key in WEATHER_RULES:
            err = validate_number_field(key, value)
            if err:
                errors.append(err)

    return errors

def handler(event, context):
    method = (((event.get("requestContext") or {}).get("http") or {}).get("method") or "").upper()
    path = event.get("rawPath", "")

    if method == "OPTIONS":
        return response(200, {"ok": True})

    if not (method == "POST" and path == "/observations"):
        return response(404, {"ok": False, "error": "route_not_found", "method": method, "path": path})

    if not auth_ok(event):
        return response(401, {"ok": False, "error": "unauthorized"})

    try:
        req = parse_body(event)
    except Exception:
        return response(400, {"ok": False, "error": "invalid_json"})

    payload = req.get("payload") or {}
    if not isinstance(payload, dict):
        return response(400, {"ok": False, "error": "payload_must_be_object"})

    weather = payload.get("weather") or {}
    source_node_id = payload.get("source_node_id")
    msg_id = payload.get("msg_id")
    received_at_utc = payload.get("received_at_utc")
    source_name = payload.get("source_name")
    source_ts_utc = payload.get("source_ts_utc")

    errors = []

    if not source_node_id or not isinstance(source_node_id, str):
        errors.append("payload.source_node_id is required and must be a string")

    if msg_id is None:
        errors.append("payload.msg_id is required")
    else:
        try:
            int(msg_id)
        except Exception:
            errors.append("payload.msg_id must be an integer")

    if not received_at_utc:
        errors.append("payload.received_at_utc is required")
    elif not is_valid_iso_utc(received_at_utc):
        errors.append("payload.received_at_utc must be ISO-8601 UTC like 2026-03-18T00:45:46.982324Z")

    if source_ts_utc is not None:
        if isinstance(source_ts_utc, str):
            if source_ts_utc.isdigit():
                pass
            elif not is_valid_iso_utc(source_ts_utc):
                errors.append("payload.source_ts_utc must be epoch string or ISO-8601 UTC string")
        elif isinstance(source_ts_utc, (int, Decimal)):
            if Decimal(str(source_ts_utc)) < 0:
                errors.append("payload.source_ts_utc epoch must be non-negative")
        else:
            errors.append("payload.source_ts_utc must be string or number")

    errors.extend(validate_weather(weather))

    if errors:
        return response(400, {"ok": False, "error": "validation_failed", "details": errors})

    pk = "STATION#" + str(source_node_id)
    padded_msg_id = pad_msg_id(msg_id)
    obs_sk = "OBS#" + str(received_at_utc) + "#" + padded_msg_id
    latest_sk = "LATEST"
    dedupe_sk = "DEDUPE#" + padded_msg_id

    observation_item = {
        "pk": pk,
        "sk": obs_sk,
        "record_type": "observation",
        "source_node_id": source_node_id,
        "source_name": source_name,
        "msg_id": int(msg_id),
        "source_ts_utc": source_ts_utc,
        "received_at_utc": received_at_utc,
        "weather": weather,
        "raw_payload": req,
        "ingested_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    latest_item = {
        "pk": pk,
        "sk": latest_sk,
        "record_type": "latest",
        "source_node_id": source_node_id,
        "source_name": source_name,
        "msg_id": int(msg_id),
        "source_ts_utc": source_ts_utc,
        "received_at_utc": received_at_utc,
        "weather": weather,
        "latest_observation_sk": obs_sk,
        "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    dedupe_item = {
        "pk": pk,
        "sk": dedupe_sk,
        "record_type": "dedupe",
        "source_node_id": source_node_id,
        "msg_id": int(msg_id),
        "received_at_utc": received_at_utc,
    }

    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {k: serializer.serialize(v) for k, v in dedupe_item.items()},
                        "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {k: serializer.serialize(v) for k, v in observation_item.items()},
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {k: serializer.serialize(v) for k, v in latest_item.items()},
                    }
                },
            ]
        )
        return response(
            200,
            {
                "ok": True,
                "deduped": False,
                "source_node_id": source_node_id,
                "msg_id": int(msg_id),
                "observation_sk": obs_sk,
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            return response(
                200,
                {
                    "ok": True,
                    "deduped": True,
                    "source_node_id": source_node_id,
                    "msg_id": int(msg_id),
                },
            )
        raise