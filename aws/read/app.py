import os
import json
import re
from decimal import Decimal
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer

ddb = boto3.client("dynamodb")
deserializer = TypeDeserializer()

TABLE_NAME = os.environ["TABLE_NAME"]
ALLOWED_CORS_ORIGIN = os.environ.get("ALLOWED_CORS_ORIGIN", "*")

NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


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


def normalize_item(item):
    out = {}
    for k, v in item.items():
        out[k] = deserializer.deserialize(v)
    return json_safe(out)


def get_query(event):
    return event.get("queryStringParameters") or {}


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_epoch_seconds(value: Decimal) -> Decimal:
    magnitude = abs(value)

    if magnitude >= Decimal("100000000000000"):  # microseconds
        return value / Decimal("1000000")

    if magnitude >= Decimal("100000000000"):  # milliseconds
        return value / Decimal("1000")

    return value  # seconds


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


def handle_get_latest(event):
    q = get_query(event)
    station_id = q.get("stationId")
    if not station_id:
        return response(400, {"ok": False, "error": "missing_stationId"})

    pk = "STATION#" + str(station_id)
    res = ddb.get_item(
        TableName=TABLE_NAME,
        Key={
            "pk": {"S": pk},
            "sk": {"S": "LATEST"},
        },
    )

    item = res.get("Item")
    if not item:
        return response(404, {"ok": False, "error": "not_found"})

    return response(200, {"ok": True, "item": normalize_item(item)})


def handle_get_history(event):
    q = get_query(event)
    station_id = q.get("stationId")
    from_ts = q.get("from")
    to_ts = q.get("to")
    limit_raw = q.get("limit", "200")

    if not station_id or not from_ts or not to_ts:
        return response(400, {"ok": False, "error": "missing_stationId_from_to"})

    try:
        limit = int(limit_raw)
    except ValueError:
        return response(400, {"ok": False, "error": "limit_must_be_integer"})

    if limit < 1 or limit > 1000:
        return response(400, {"ok": False, "error": "limit_must_be_between_1_and_1000"})

    try:
        from_sort_utc = normalize_timestamp_to_sortable_utc(from_ts, "from")
        to_sort_utc = normalize_timestamp_to_sortable_utc(to_ts, "to")
    except ValueError as e:
        return response(400, {"ok": False, "error": "invalid_time_range", "details": [str(e)]})

    if from_sort_utc > to_sort_utc:
        return response(400, {"ok": False, "error": "from_must_be_less_than_or_equal_to_to"})

    pk = "STATION#" + str(station_id)
    sk_from = "OBS#" + from_sort_utc + "#"
    sk_to = "OBS#" + to_sort_utc + "~"

    res = ddb.query(
        TableName=TABLE_NAME,
        KeyConditionExpression="pk = :pk AND sk BETWEEN :from AND :to",
        ExpressionAttributeValues={
            ":pk": {"S": pk},
            ":from": {"S": sk_from},
            ":to": {"S": sk_to},
        },
        Limit=limit,
        ScanIndexForward=False,
    )

    items = [normalize_item(x) for x in res.get("Items", [])]
    return response(200, {"ok": True, "count": len(items), "items": items})


def handler(event, context):
    method = (((event.get("requestContext") or {}).get("http") or {}).get("method") or "").upper()
    path = event.get("rawPath", "")

    if method == "OPTIONS":
        return response(200, {"ok": True})

    if method == "GET" and path == "/observations/latest":
        return handle_get_latest(event)

    if method == "GET" and path == "/observations":
        return handle_get_history(event)

    return response(404, {"ok": False, "error": "route_not_found", "method": method, "path": path})