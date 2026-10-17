import os
import json
import base64
import re

import boto3
from decimal import Decimal
from datetime import datetime, timezone
from boto3.dynamodb.types import TypeDeserializer

ddb = boto3.client("dynamodb")
deserializer = TypeDeserializer()

TABLE_NAME = os.environ["TABLE_NAME"]
ALLOWED_CORS_ORIGIN = os.environ.get("ALLOWED_CORS_ORIGIN", "*")
# Enable only after the index is ACTIVE and existing observations are backfilled.
HISTORY_INDEX_NAME = os.environ.get("HISTORY_INDEX_NAME", "")

RAW_LIMIT_DEFAULT = 200
RAW_LIMIT_MAX = 1000
SAMPLE_MAX = 2000
SAMPLE_SCAN_PAGE_SIZE = 1000
SAMPLE_MAX_SCAN_ITEMS = 50000
SOURCE_CLOCK_SKEW_THRESHOLD_SECONDS = Decimal("600")
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
    return add_timestamp_metadata(out)


def parse_sortable_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_epoch_seconds(value):
    magnitude = abs(value)
    if magnitude >= Decimal("100000000000000"):
        return value / Decimal("1000000")
    if magnitude >= Decimal("100000000000"):
        return value / Decimal("1000")
    return value


def normalize_history_bound(value):
    raw = str(value).strip()
    try:
        if NUMERIC_RE.match(raw):
            numeric_value = normalize_epoch_seconds(Decimal(raw))
            if numeric_value < 0:
                return raw
            dt = datetime.fromtimestamp(float(numeric_value), tz=timezone.utc)
        else:
            dt = parse_sortable_utc(raw)
            if dt.tzinfo is None:
                return raw
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ArithmeticError, OSError, OverflowError, TypeError, ValueError):
        # Preserve the previous pass-through behavior for non-timestamp bounds.
        return raw


def clock_skew_seconds(received_at_utc, source_ts_utc):
    delta = parse_sortable_utc(received_at_utc) - parse_sortable_utc(source_ts_utc)
    return (
        Decimal(delta.days * 86400 + delta.seconds)
        + (Decimal(delta.microseconds) / Decimal("1000000"))
    )


def add_timestamp_metadata(item):
    source_ts_utc = item.get("source_ts_utc") or item.get("source_ts_sort_utc")
    received_at_utc = item.get("received_at_utc")

    source_clock_skew_seconds = item.get("source_clock_skew_seconds")
    if source_clock_skew_seconds is None and source_ts_utc and received_at_utc:
        try:
            source_clock_skew_seconds = clock_skew_seconds(
                received_at_utc,
                source_ts_utc,
            )
        except (TypeError, ValueError):
            source_clock_skew_seconds = None

    if source_clock_skew_seconds is not None:
        item["source_clock_skew_seconds"] = source_clock_skew_seconds

    source_time_suspect = bool(item.get("source_time_suspect", False))
    if (
        source_clock_skew_seconds is not None
        and abs(source_clock_skew_seconds) > SOURCE_CLOCK_SKEW_THRESHOLD_SECONDS
    ):
        source_time_suspect = True
    item["source_time_suspect"] = source_time_suspect

    if "ingested_at_utc" not in item and item.get("updated_at_utc"):
        item["ingested_at_utc"] = item["updated_at_utc"]

    if source_time_suspect and received_at_utc:
        item["display_ts_utc"] = received_at_utc
    elif not item.get("display_ts_utc"):
        item["display_ts_utc"] = (
            source_ts_utc
            or received_at_utc
            or item.get("ingested_at_utc")
        )

    return item


def build_display_history_sort(item):
    """Index the same effective instant returned to clients, retaining row identity."""
    normalized = add_timestamp_metadata(dict(item))
    timestamp = normalize_history_bound(normalized["display_ts_utc"])
    parsed = parse_sortable_utc(timestamp)
    if parsed.tzinfo is None:
        raise ValueError("display_timestamp_requires_timezone")
    return f"DISPLAY#{timestamp}#{item['sk']}"


def as_decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def first_weather_number(weather, keys):
    if not isinstance(weather, dict):
        return None

    for key in keys:
        value = as_decimal(weather.get(key))
        if value is not None:
            return value

    return None


def build_history_summary(items):
    rain_total_mm = Decimal("0")
    rain_reported_interval_count = 0
    rain_event_items = []

    lightning_reported_point_count = 0
    lightning_active_points = 0
    lightning_total_strikes = Decimal("0")
    lightning_weighted_distance_total_km = Decimal("0")
    lightning_weighted_strike_count = Decimal("0")
    lightning_closest_distance_km = None

    for item in items:
        weather = item.get("weather")
        if not isinstance(weather, dict):
            continue

        rain_interval_mm = first_weather_number(
            weather,
            ("rain_interval_mm", "rain_mm"),
        )
        if rain_interval_mm is not None:
            rain_reported_interval_count += 1
            if rain_interval_mm > 0:
                rain_total_mm += rain_interval_mm
                rain_event_items.append(item)

        strike_count = first_weather_number(
            weather,
            ("lightning_strike_count",),
        )
        if strike_count is None:
            continue

        lightning_reported_point_count += 1
        if strike_count <= 0:
            continue

        lightning_active_points += 1
        lightning_total_strikes += strike_count

        distance_km = first_weather_number(
            weather,
            ("lightning_avg_distance_km",),
        )
        if distance_km is None or distance_km < 0:
            continue

        lightning_weighted_distance_total_km += distance_km * strike_count
        lightning_weighted_strike_count += strike_count
        lightning_closest_distance_km = (
            distance_km
            if lightning_closest_distance_km is None
            else min(lightning_closest_distance_km, distance_km)
        )

    rain_summary = None
    if rain_reported_interval_count:
        rain_summary = {
            "eventCount": len(rain_event_items),
            "eventItems": rain_event_items,
            "reportedIntervalCount": rain_reported_interval_count,
            "totalMm": rain_total_mm,
        }

    lightning_summary = None
    if lightning_reported_point_count:
        lightning_summary = {
            "activePoints": lightning_active_points,
            "averageDistanceKm": (
                lightning_weighted_distance_total_km / lightning_weighted_strike_count
                if lightning_weighted_strike_count > 0
                else None
            ),
            "closestDistanceKm": lightning_closest_distance_km,
            "reportedPointCount": lightning_reported_point_count,
            "totalStrikes": lightning_total_strikes,
        }

    return {
        "rain": rain_summary,
        "lightning": lightning_summary,
    }


def get_query(event):
    return event.get("queryStringParameters") or {}


def parse_int(name, raw_value, min_value=None, max_value=None):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}_must_be_integer")

    if min_value is not None and value < min_value:
        raise ValueError(f"{name}_must_be_at_least_{min_value}")

    if max_value is not None and value > max_value:
        raise ValueError(f"{name}_must_be_at_most_{max_value}")

    return value


def encode_next_token(last_evaluated_key):
    if not last_evaluated_key:
        return None
    raw = json.dumps(last_evaluated_key).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def decode_next_token(token):
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("invalid_nextToken")


def query_history_page(
    pk,
    sk_from,
    sk_to,
    *,
    limit=None,
    ascending=False,
    exclusive_start_key=None,
    projection=False,
):
    kwargs = {
        "TableName": TABLE_NAME,
        "KeyConditionExpression": "pk = :pk AND #history_key BETWEEN :from AND :to",
        "ExpressionAttributeNames": {
            "#history_key": "history_sort" if HISTORY_INDEX_NAME else "sk",
        },
        "ExpressionAttributeValues": {
            ":pk": {"S": pk},
            ":from": {"S": sk_from},
            ":to": {"S": sk_to},
        },
        "ScanIndexForward": ascending,
    }

    if HISTORY_INDEX_NAME:
        kwargs["IndexName"] = HISTORY_INDEX_NAME

    if limit is not None:
        kwargs["Limit"] = limit

    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    if projection:
        kwargs["ProjectionExpression"] = (
            "pk, sk, record_type, observation_type, source_node_id, source_name, "
            "msg_id, source_ts_utc, source_ts_sort_utc, received_at_utc, "
            "ingested_at_utc, display_ts_utc, source_time_suspect, "
            "source_clock_skew_seconds, weather"
        )

    return ddb.query(**kwargs)


def evenly_sample_items(items, sample_size):
    total = len(items)

    if sample_size >= total:
        return items

    if sample_size == 1:
        return [items[total // 2]]

    selected_indices = []
    seen = set()

    for i in range(sample_size):
        idx = round(i * (total - 1) / (sample_size - 1))
        if idx not in seen:
            selected_indices.append(idx)
            seen.add(idx)

    if len(selected_indices) < sample_size:
        for idx in range(total):
            if idx not in seen:
                selected_indices.append(idx)
                seen.add(idx)
                if len(selected_indices) == sample_size:
                    break
        selected_indices.sort()

    return [items[idx] for idx in selected_indices]


def fetch_all_items_for_sampling(pk, sk_from, sk_to, max_scan_items=SAMPLE_MAX_SCAN_ITEMS):
    items = []
    last_evaluated_key = None

    while True:
        res = query_history_page(
            pk,
            sk_from,
            sk_to,
            limit=SAMPLE_SCAN_PAGE_SIZE,
            ascending=True,
            exclusive_start_key=last_evaluated_key,
            projection=True,
        )

        page_items = [normalize_item(x) for x in res.get("Items", [])]
        items.extend(page_items)

        if len(items) > max_scan_items:
            raise ValueError("sample_window_too_large")

        last_evaluated_key = res.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

    return items


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


def handle_get_chart(station_id, from_ts, to_ts, max_points):
    # Opt-in keeps raw and sampled clients unchanged during a staged rollout.
    rollup_table = os.environ.get("ROLLUP_TABLE_NAME", "")
    if os.environ.get("AGGREGATE_HISTORY_ENABLED") != "true" or not rollup_table or not HISTORY_INDEX_NAME:
        return response(503, {"ok": False, "error": "history_aggregates_not_ready"})
    try:
        count = parse_int("maxPoints", max_points, 1, 2000)
        from aggregate_history import handle_chart_query
        from botocore.config import Config
        chart_ddb = boto3.client("dynamodb", config=Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 1}))
        body = handle_chart_query(chart_ddb, TABLE_NAME, rollup_table, str(station_id), from_ts, to_ts, count)
        return response(200, body)
    except ValueError as exc:
        error = str(exc)
        status = 503 if error in ("history_aggregates_not_ready", "history_query_budget_exceeded") else 400
        return response(status, {"ok": False, "error": error})
    except Exception as exc:
        print(json.dumps({"operation": "chart_history", "errorType": type(exc).__name__}))
        return response(503, {"ok": False, "error": "history_temporarily_unavailable"})


def handle_get_history(event):
    q = get_query(event)

    station_id = q.get("stationId")
    from_ts = q.get("from")
    to_ts = q.get("to")

    if not station_id or not from_ts or not to_ts:
        return response(400, {"ok": False, "error": "missing_stationId_from_to"})

    if q.get("view") == "chart":
        return handle_get_chart(station_id, from_ts, to_ts, q.get("maxPoints", "800"))

    limit_raw = q.get("limit", str(RAW_LIMIT_DEFAULT))
    sample_raw = q.get("sample")
    next_token_raw = q.get("nextToken")
    order = (q.get("order") or "").lower()

    try:
        limit = parse_int("limit", limit_raw, 1, RAW_LIMIT_MAX)
    except ValueError as exc:
        return response(400, {"ok": False, "error": str(exc)})

    sample = None
    if sample_raw is not None:
        try:
            sample = parse_int("sample", sample_raw, 1, SAMPLE_MAX)
        except ValueError as exc:
            return response(400, {"ok": False, "error": str(exc)})

    try:
        exclusive_start_key = decode_next_token(next_token_raw) if next_token_raw else None
        if exclusive_start_key is not None and (
            not isinstance(exclusive_start_key, dict)
            or not all(
                isinstance(exclusive_start_key.get(key), dict)
                and isinstance(exclusive_start_key[key].get("S"), str)
                for key in (["pk", "sk", "history_sort"] if HISTORY_INDEX_NAME else ["pk", "sk"])
            )
        ):
            raise ValueError("invalid_nextToken")
    except ValueError as exc:
        return response(400, {"ok": False, "error": str(exc)})

    pk = "STATION#" + str(station_id)
    history_prefix = "DISPLAY#" if HISTORY_INDEX_NAME else "OBS#"
    sk_from = history_prefix + normalize_history_bound(from_ts)
    sk_to = history_prefix + normalize_history_bound(to_ts) + "~"

    if sample is not None:
        try:
            all_items = fetch_all_items_for_sampling(pk, sk_from, sk_to)
        except ValueError as exc:
            return response(400, {"ok": False, "error": str(exc)})

        sampled_items = evenly_sample_items(all_items, sample)
        summary = build_history_summary(all_items)

        return response(
            200,
            {
                "ok": True,
                "mode": "sampled",
                "stationId": station_id,
                "from": from_ts,
                "to": to_ts,
                "sampleRequested": sample,
                "totalMatched": len(all_items),
                "count": len(sampled_items),
                "items": sampled_items,
                "summary": summary,
            },
        )

    ascending = order == "asc"

    res = query_history_page(
        pk,
        sk_from,
        sk_to,
        limit=limit,
        ascending=ascending,
        exclusive_start_key=exclusive_start_key,
        projection=False,
    )

    items = [normalize_item(x) for x in res.get("Items", [])]
    next_token = encode_next_token(res.get("LastEvaluatedKey"))

    body = {
        "ok": True,
        "mode": "raw",
        "stationId": station_id,
        "from": from_ts,
        "to": to_ts,
        "order": "asc" if ascending else "desc",
        "count": len(items),
        "items": items,
    }

    if next_token:
        body["nextToken"] = next_token

    return response(200, body)


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
