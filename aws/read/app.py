import os
import json
import base64

import boto3
from decimal import Decimal
from boto3.dynamodb.types import TypeDeserializer

ddb = boto3.client("dynamodb")
deserializer = TypeDeserializer()

TABLE_NAME = os.environ["TABLE_NAME"]
ALLOWED_CORS_ORIGIN = os.environ.get("ALLOWED_CORS_ORIGIN", "*")

RAW_LIMIT_DEFAULT = 200
RAW_LIMIT_MAX = 1000
SAMPLE_MAX = 2000
SAMPLE_SCAN_PAGE_SIZE = 1000
SAMPLE_MAX_SCAN_ITEMS = 50000


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
        "KeyConditionExpression": "pk = :pk AND sk BETWEEN :from AND :to",
        "ExpressionAttributeValues": {
            ":pk": {"S": pk},
            ":from": {"S": sk_from},
            ":to": {"S": sk_to},
        },
        "ScanIndexForward": ascending,
    }

    if limit is not None:
        kwargs["Limit"] = limit

    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    if projection:
        kwargs["ProjectionExpression"] = (
            "pk, sk, record_type, source_node_id, source_name, msg_id, "
            "source_ts_utc, received_at_utc, weather"
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


def handle_get_history(event):
    q = get_query(event)

    station_id = q.get("stationId")
    from_ts = q.get("from")
    to_ts = q.get("to")

    if not station_id or not from_ts or not to_ts:
        return response(400, {"ok": False, "error": "missing_stationId_from_to"})

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
    except ValueError as exc:
        return response(400, {"ok": False, "error": str(exc)})

    pk = "STATION#" + str(station_id)
    sk_from = "OBS#" + str(from_ts)
    sk_to = "OBS#" + str(to_ts) + "~"

    if sample is not None:
        try:
            all_items = fetch_all_items_for_sampling(pk, sk_from, sk_to)
        except ValueError as exc:
            return response(400, {"ok": False, "error": str(exc)})

        sampled_items = evenly_sample_items(all_items, sample)

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