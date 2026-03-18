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

NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

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


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def normalize_epoch_seconds(value: Decimal) -> Decimal:
    magnitude = abs(value)

    if magnitude >= Decimal("100000000000000"):  # microseconds
        return value / Decimal("1000000")

    if magnitude >= Decimal("100000000000"):  # milliseconds
        return value / Decimal("1000")

    return value  # seconds


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_timestamp_to_sortable_utc(value, field_name: str) -> str:
    if value is None:
        raise ValueError(f"{field_name} is required")

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an ISO-8601 UTC string or epoch number")

    if isinstance(value, (int, Decimal)):
        numeric_value = normalize_epoch_seconds(Decimal(str(value)))
        if numeric_value < 0:
            raise ValueError(f"{field_name} epoch must be non-negative")
        try:
            dt = datetime.fromtimestamp(float(numeric_value), tz=timezone.utc)
        except Exception:
            raise ValueError(f"{field_name} epoch is out of range")
        return format_utc(dt)

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field_name} must not be empty")

        if NUMERIC_RE.match(raw):
            numeric_value = normalize_epoch_seconds(Decimal(raw))
            if numeric_value < 0:
                raise ValueError(f"{field_name} epoch must be non-negative")
            try:
                dt = datetime.fromtimestamp(float(numeric_value), tz=timezone.utc)
            except Exception:
                raise ValueError(f"{field_name} epoch is out of range")
            return format_utc(dt)

        iso_candidate = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_candidate)
        except ValueError:
            raise ValueError(
                f"{field_name} must be an ISO-8601 UTC string like 2026-03-18T00:45:46.982324Z "
                f"or an epoch number"
            )

        if dt.tzinfo is None:
            raise ValueError(f"{field_name} must include timezone information")
        return format_utc(dt)

    raise ValueError(f"{field_name} must be an ISO-8601 UTC string or epoch number")


def validate_number_field(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
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
            try:
                normalize_timestamp_to_sortable_utc(value, "weather.timestamp")
            except ValueError as e:
                errors.append(str(e))
            continue

        if key in WEATHER_RULES:
            err = validate_number_field(key, value)
            if err:
                errors.append(err)

    return errors


def serialize_item(item: dict) -> dict:
    return {k: serializer.serialize(v) for k, v in item.items()}


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

    weather = payload.get("weather")
    source_node_id = payload.get("source_node_id")
    msg_id = payload.get("msg_id")
    received_at_utc = payload.get("received_at_utc")
    source_name = payload.get("source_name")
    source_ts_raw = payload.get("source_ts_utc")

    errors = []

    if not source_node_id or not isinstance(source_node_id, str):
        errors.append("payload.source_node_id is required and must be a string")

    if msg_id is None:
        errors.append("payload.msg_id is required")
    else:
        try:
            msg_id = int(msg_id)
        except Exception:
            errors.append("payload.msg_id must be an integer")

    if weather is None:
        errors.append("payload.weather is required")
        weather = {}
    elif not isinstance(weather, dict):
        errors.append("payload.weather must be an object")

    try:
        received_at_sort_utc = normalize_timestamp_to_sortable_utc(
            received_at_utc,
            "payload.received_at_utc",
        )
    except ValueError as e:
        errors.append(str(e))
        received_at_sort_utc = None

    try:
        source_ts_sort_utc = normalize_timestamp_to_sortable_utc(
            source_ts_raw,
            "payload.source_ts_utc",
        )
    except ValueError as e:
        errors.append(str(e))
        source_ts_sort_utc = None

    errors.extend(validate_weather(weather))

    if errors:
        return response(400, {"ok": False, "error": "validation_failed", "details": errors})

    pk = "STATION#" + str(source_node_id)
    obs_sk = f"OBS#{source_ts_sort_utc}#WEATHER"
    latest_sk = "LATEST"
    ingested_at_utc = utc_now_iso()

    observation_item = {
        "pk": pk,
        "sk": obs_sk,
        "record_type": "observation",
        "observation_type": "WEATHER",
        "source_node_id": source_node_id,
        "source_name": source_name,
        "msg_id": msg_id,
        "source_ts_utc": source_ts_sort_utc,
        "source_ts_raw": source_ts_raw,
        "source_ts_sort_utc": source_ts_sort_utc,
        "received_at_utc": received_at_sort_utc,
        "weather": weather,
        "raw_payload": req,
        "ingested_at_utc": ingested_at_utc,
    }

    latest_item = {
        "pk": pk,
        "sk": latest_sk,
        "record_type": "latest",
        "observation_type": "WEATHER",
        "source_node_id": source_node_id,
        "source_name": source_name,
        "msg_id": msg_id,
        "source_ts_utc": source_ts_sort_utc,
        "source_ts_raw": source_ts_raw,
        "source_ts_sort_utc": source_ts_sort_utc,
        "received_at_utc": received_at_sort_utc,
        "weather": weather,
        "latest_observation_sk": obs_sk,
        "updated_at_utc": utc_now_iso(),
    }

    try:
        ddb.put_item(
            TableName=TABLE_NAME,
            Item=serialize_item(observation_item),
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return response(
                200,
                {
                    "ok": True,
                    "deduped": True,
                    "source_node_id": source_node_id,
                    "msg_id": msg_id,
                    "source_ts_utc": source_ts_sort_utc,
                    "observation_sk": obs_sk,
                },
            )
        raise

    try:
        ddb.put_item(
            TableName=TABLE_NAME,
            Item=serialize_item(latest_item),
            ConditionExpression="attribute_not_exists(pk) OR source_ts_sort_utc <= :incoming_ts",
            ExpressionAttributeValues={
                ":incoming_ts": serializer.serialize(source_ts_sort_utc),
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "ConditionalCheckFailedException":
            raise
        # Older out-of-order observation. Keep history row, but do not move LATEST backwards.

    return response(
        200,
        {
            "ok": True,
            "deduped": False,
            "source_node_id": source_node_id,
            "msg_id": msg_id,
            "source_ts_utc": source_ts_sort_utc,
            "observation_sk": obs_sk,
        },
    )