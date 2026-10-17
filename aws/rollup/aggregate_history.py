"""Bounded history reads and mergeable, observation-weighted weather summaries.

All bucket boundaries are UTC, all timestamp decisions use the existing display
policy, and persisted aggregates can be rebuilt without incrementing counters.
Only the DynamoDB encoding helpers require boto3.
"""
import math
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

UTC = timezone.utc
ZERO = Decimal("0")
LEVELS = ("hour", "day", "month", "year")
MAX_RAW_HOUR_ROWS = 5000
MAX_QUERY_ROWS = 12000
MAX_QUERY_CALLS = 120
MAX_QUERY_SECONDS = 15
NUMERIC_TIMESTAMP = re.compile(r"^-?\d+(?:\.\d+)?$")


def parse_time(value):
    if isinstance(value, datetime):
        result = value
    else:
        raw = str(value).strip()
        if isinstance(value, bool):
            raise ValueError("invalid_history_timestamp")
        if isinstance(value, (int, Decimal, float)) or NUMERIC_TIMESTAMP.fullmatch(raw):
            numeric = number(value)
            if numeric is None or numeric < 0:
                raise ValueError("invalid_history_epoch_timestamp")
            if abs(numeric) >= Decimal("100000000000000"):
                numeric /= Decimal("1000000")
            elif abs(numeric) >= Decimal("100000000000"):
                numeric /= Decimal("1000")
            try:
                result = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(numeric * 1000000))
            except (OverflowError, ValueError):
                raise ValueError("history_epoch_timestamp_out_of_range") from None
        else:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("history_timestamp_requires_timezone")
    return result.astimezone(UTC)


def iso(value):
    return parse_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def effective_time(item):
    # Existing legacy rows sometimes retain epoch values in *_utc fields.
    # The deployed display index is the authoritative selected instant; using
    # it prevents a later reinterpretation of legacy clock fields from moving
    # an observation into a bucket different from its indexed query location.
    indexed = item.get("history_sort")
    if isinstance(indexed, str) and indexed.startswith("DISPLAY#"):
        try:
            return iso(indexed.split("#", 2)[1])
        except (TypeError, ValueError):
            pass
    source = item.get("source_ts_utc") or item.get("source_ts_sort_utc")
    received = item.get("received_at_utc")
    suspect = bool(item.get("source_time_suspect"))
    skew = number(item.get("source_clock_skew_seconds"))
    if skew is None and source and received:
        try:
            skew = Decimal(str((parse_time(received) - parse_time(source)).total_seconds()))
        except (TypeError, ValueError):
            pass
    suspect = suspect or (skew is not None and abs(skew) > 600)
    value = received if suspect and received else (
        item.get("display_ts_utc") or source or received or
        item.get("ingested_at_utc") or item.get("updated_at_utc")
    )
    return iso(value)


def floor_bucket(value, resolution):
    value = parse_time(value)
    if resolution == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if resolution == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if resolution == "month":
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if resolution == "year":
        return value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("invalid_history_resolution")


def next_bucket(start, resolution):
    start = floor_bucket(start, resolution)
    if resolution == "hour":
        return start + timedelta(hours=1)
    if resolution == "day":
        return start + timedelta(days=1)
    if resolution == "month":
        return start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    return start.replace(year=start.year + 1)


def bucket_count(start, end, resolution):
    first = floor_bucket(start, resolution)
    last = floor_bucket(end - timedelta(microseconds=1), resolution)
    if resolution == "hour":
        return int((last - first).total_seconds() // 3600) + 1
    if resolution == "day":
        return (last - first).days + 1
    if resolution == "month":
        return (last.year - first.year) * 12 + last.month - first.month + 1
    return last.year - first.year + 1


def empty_aggregate():
    return {
        "count": 0, "metrics": {}, "first": None, "last": None,
        "windSin": ZERO, "windCos": ZERO,
        "rain": {"totalMm": ZERO, "reportedIntervalCount": 0, "eventCount": 0},
        "lightning": {"totalStrikes": ZERO, "reportedPointCount": 0,
                      "activePoints": 0, "weightedDistance": ZERO,
                      "distanceStrikeCount": ZERO, "closestDistanceKm": None},
    }


def summarize_observations(items):
    result = empty_aggregate()
    for item in items:
        stamp = effective_time(item)
        row_key = str(item.get("sk", ""))
        result["count"] += 1
        result["first"] = min(result["first"] or stamp, stamp)
        result["last"] = max(result["last"] or stamp, stamp)
        weather = item.get("weather") or {}
        for key, raw_value in weather.items():
            value = number(raw_value)
            if value is None:
                continue
            metric = result["metrics"].setdefault(key, {
                "sum": ZERO, "count": 0, "min": value, "max": value,
                "last": value, "lastAt": stamp, "lastKey": row_key,
            })
            metric["sum"] += value
            metric["count"] += 1
            metric["min"] = min(metric["min"], value)
            metric["max"] = max(metric["max"], value)
            if (stamp, row_key) >= (metric["lastAt"], metric["lastKey"]):
                metric.update(last=value, lastAt=stamp, lastKey=row_key)
            if key == "wind_dir_deg":
                result["windSin"] += Decimal(str(math.sin(math.radians(float(value)))))
                result["windCos"] += Decimal(str(math.cos(math.radians(float(value)))))
        rain = number(weather.get("rain_interval_mm"))
        if rain is None:
            rain = number(weather.get("rain_mm"))
        if rain is not None:
            result["rain"]["reportedIntervalCount"] += 1
            if rain > 0:
                result["rain"]["totalMm"] += rain
                result["rain"]["eventCount"] += 1
        strikes = number(weather.get("lightning_strike_count"))
        if strikes is not None:
            lightning = result["lightning"]
            lightning["reportedPointCount"] += 1
            if strikes > 0:
                lightning["totalStrikes"] += strikes
                lightning["activePoints"] += 1
                distance = number(weather.get("lightning_avg_distance_km"))
                if distance is not None and distance >= 0:
                    lightning["weightedDistance"] += distance * strikes
                    lightning["distanceStrikeCount"] += strikes
                    current = lightning["closestDistanceKm"]
                    lightning["closestDistanceKm"] = distance if current is None else min(current, distance)
    return result


def merge_aggregates(aggregates):
    result = empty_aggregate()
    for aggregate in aggregates:
        if not aggregate or not aggregate["count"]:
            continue
        result["count"] += aggregate["count"]
        result["first"] = min(result["first"] or aggregate["first"], aggregate["first"])
        result["last"] = max(result["last"] or aggregate["last"], aggregate["last"])
        result["windSin"] += aggregate.get("windSin", ZERO)
        result["windCos"] += aggregate.get("windCos", ZERO)
        for key, source in aggregate["metrics"].items():
            if key not in result["metrics"]:
                result["metrics"][key] = dict(source)
                continue
            target = result["metrics"][key]
            target["sum"] += source["sum"]
            target["count"] += source["count"]
            target["min"] = min(target["min"], source["min"])
            target["max"] = max(target["max"], source["max"])
            if (source["lastAt"], source.get("lastKey", "")) >= (target["lastAt"], target.get("lastKey", "")):
                for field in ("last", "lastAt", "lastKey"):
                    target[field] = source.get(field, "")
        for key in result["rain"]:
            result["rain"][key] += aggregate["rain"][key]
        for key in result["lightning"]:
            source = aggregate["lightning"][key]
            if key == "closestDistanceKm":
                current = result["lightning"][key]
                if source is not None:
                    result["lightning"][key] = source if current is None else min(current, source)
            else:
                result["lightning"][key] += source
    return result


def public_summary(aggregate):
    metrics = {key: {name: value for name, value in metric.items() if name != "lastKey"}
               for key, metric in aggregate["metrics"].items()}
    lightning = aggregate["lightning"]
    denominator = lightning["distanceStrikeCount"]
    return {
        "metrics": metrics,
        "rain": dict(aggregate["rain"]) if aggregate["rain"]["reportedIntervalCount"] else None,
        "lightning": {
            "totalStrikes": lightning["totalStrikes"],
            "reportedPointCount": lightning["reportedPointCount"],
            "activePoints": lightning["activePoints"],
            "averageDistanceKm": lightning["weightedDistance"] / denominator if denominator else None,
            "closestDistanceKm": lightning["closestDistanceKm"],
        } if lightning["reportedPointCount"] else None,
    }


def weather_values(aggregate):
    values = {key: metric["sum"] / metric["count"]
              for key, metric in aggregate["metrics"].items() if metric["count"]}
    for key in ("local_day_rain_mm", "local_day_nearcast_rain_mm", "precipitation_type", "precipitation_analysis_type"):
        if key in values:
            values[key] = aggregate["metrics"][key]["last"]
    if "wind_gust_ms" in values:
        values["wind_gust_ms"] = aggregate["metrics"]["wind_gust_ms"]["max"]
    if "wind_dir_deg" in values:
        sine, cosine = float(aggregate["windSin"]), float(aggregate["windCos"])
        angle = math.degrees(math.atan2(sine, cosine)) % 360
        values["wind_dir_deg"] = None if math.hypot(sine, cosine) < 1e-10 else Decimal(str(0 if abs(angle - 360) < 1e-9 else angle))
    if aggregate["rain"]["reportedIntervalCount"]:
        values["rain_interval_mm"] = aggregate["rain"]["totalMm"]
        if "rain_mm" in values:
            values["rain_mm"] = aggregate["rain"]["totalMm"]
    lightning = aggregate["lightning"]
    if lightning["reportedPointCount"]:
        values["lightning_strike_count"] = lightning["totalStrikes"]
        values["lightning_avg_distance_km"] = (
            lightning["weightedDistance"] / lightning["distanceStrikeCount"]
            if lightning["distanceStrikeCount"] else None
        )
    return values


def encode_item(item):
    from boto3.dynamodb.types import TypeSerializer
    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def decode_item(item):
    from boto3.dynamodb.types import TypeDeserializer
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def rollup_row(station_id, resolution, start, aggregate, updated_at=None):
    start = floor_bucket(start, resolution)
    return {
        "pk": f"STATION#{station_id}#ROLLUP#{resolution}", "sk": iso(start),
        "resolution": resolution, "bucketStart": iso(start),
        "bucketEnd": iso(next_bucket(start, resolution)), "aggregate": aggregate,
        "updatedAt": updated_at or iso(datetime.now(UTC)), "schemaVersion": 1,
    }


class QueryBudget:
    def __init__(self, rows=MAX_QUERY_ROWS, calls=MAX_QUERY_CALLS, seconds=MAX_QUERY_SECONDS):
        import time
        self.rows, self.calls = rows, calls
        self.deadline = time.monotonic() + seconds

    def before(self, limit):
        import time
        if self.rows <= 0 or self.calls <= 0 or time.monotonic() >= self.deadline:
            raise ValueError("history_query_budget_exceeded")
        self.calls -= 1
        return min(limit, self.rows)


def query_rows(ddb, table, pk, start_key, end_key, budget, cap=None, index=None, truncate=False):
    rows, last_key = [], None
    while True:
        wanted = min(500, cap - len(rows)) if cap is not None else 500
        if wanted <= 0:
            if truncate:
                return rows, True
            raise ValueError("history_query_budget_exceeded")
        limit = budget.before(wanted)
        args = {
            "TableName": table, "KeyConditionExpression": "pk = :pk AND #sort BETWEEN :lo AND :hi",
            "ExpressionAttributeNames": {"#sort": "history_sort" if index else "sk"},
            "ExpressionAttributeValues": {":pk": {"S": pk}, ":lo": {"S": start_key}, ":hi": {"S": end_key}},
            "ScanIndexForward": True, "Limit": limit,
        }
        if index:
            args["IndexName"] = index
        else:
            args["ConsistentRead"] = True
        if last_key:
            args["ExclusiveStartKey"] = last_key
        response = ddb.query(**args)
        raw = response.get("Items", [])
        budget.rows -= int(response.get("ScannedCount", len(raw)))
        rows.extend(decode_item(item) for item in raw)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return rows, False


def fetch_raw(ddb, table, station_id, start, end, budget, cap=MAX_RAW_HOUR_ROWS, truncate=False):
    end_inclusive = end - timedelta(microseconds=1)
    rows, more = query_rows(
        ddb, table, f"STATION#{station_id}", "DISPLAY#" + iso(start),
        "DISPLAY#" + iso(end_inclusive) + "~", budget, cap=cap,
        index=os.environ.get("HISTORY_INDEX_NAME") or "ObservationDisplayTimeIndex", truncate=truncate,
    )
    for row in rows:
        row["display_ts_utc"] = effective_time(row)
    return rows, more


def fetch_rollups(ddb, table, station_id, resolution, start, end, budget):
    rows, _ = query_rows(
        ddb, table, f"STATION#{station_id}#ROLLUP#{resolution}",
        iso(floor_bucket(start, resolution)), iso(floor_bucket(end - timedelta(microseconds=1), resolution)), budget,
    )
    return rows


def read_exact(ddb, observations, rollups, station_id, resolution, start, end, budget):
    """Replace partially selected buckets with exact smaller-bucket summaries."""
    if end <= start:
        return []
    rows = fetch_rollups(ddb, rollups, station_id, resolution, start, end, budget)
    result = []
    for row in rows:
        bucket_start, bucket_end = parse_time(row["bucketStart"]), parse_time(row["bucketEnd"])
        clipped_start, clipped_end = max(start, bucket_start), min(end, bucket_end)
        if clipped_start == bucket_start and clipped_end == bucket_end:
            aggregate = row["aggregate"]
        elif resolution == "hour":
            raw, _ = fetch_raw(ddb, observations, station_id, clipped_start, clipped_end, budget)
            aggregate = summarize_observations(raw)
        else:
            child_level = LEVELS[LEVELS.index(resolution) - 1]
            children = read_exact(ddb, observations, rollups, station_id, child_level, clipped_start, clipped_end, budget)
            aggregate = merge_aggregates(child[2] for child in children)
        if aggregate["count"]:
            result.append((clipped_start, clipped_end, aggregate))
    return result


def handle_chart_query(ddb, observation_table, rollup_table, station_id, from_ts, to_ts, max_points):
    start, inclusive_end = parse_time(from_ts), parse_time(to_ts)
    if start > inclusive_end:
        raise ValueError("history_from_must_precede_to")
    if not isinstance(max_points, int) or isinstance(max_points, bool) or not 1 <= max_points <= 2000:
        raise ValueError("maxPoints_must_be_between_1_and_2000")
    # A year-9999 bucket cannot have a representable exclusive end.
    if inclusive_end.year >= 9999:
        inclusive_end = datetime(9998, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    meta_response = ddb.get_item(TableName=rollup_table, Key=encode_item({"pk": f"STATION#{station_id}", "sk": "META"}), ConsistentRead=True)
    if not meta_response.get("Item"):
        raise ValueError("history_aggregates_not_ready")
    meta = decode_item(meta_response["Item"])
    result = {
        "ok": True, "mode": "aggregate", "stationId": station_id,
        "from": from_ts, "to": to_ts, "resolution": "hour", "maxPoints": max_points,
        "totalMatched": 0, "count": 0, "items": [], "summary": public_summary(empty_aggregate()),
        "coverage": {"availableFrom": meta.get("availableFrom"), "availableTo": meta.get("availableTo"), "first": None, "last": None},
        "updatedAt": meta.get("updatedAt"), "timestampBasis": "display",
    }
    if not meta.get("availableFrom") or not meta.get("availableTo"):
        return result
    start = max(start, parse_time(meta["availableFrom"]))
    end = min(inclusive_end + timedelta(microseconds=1), parse_time(meta["availableTo"]) + timedelta(microseconds=1))
    if end <= start:
        return result
    budget = QueryBudget()
    if end - start <= timedelta(hours=24):
        raw, more = fetch_raw(ddb, observation_table, station_id, start, end, budget, max_points + 1, truncate=True)
        if not more and len(raw) <= max_points:
            aggregate = summarize_observations(raw)
            result.update(mode="detail", resolution="raw", items=raw, count=len(raw), totalMatched=len(raw), summary=public_summary(aggregate))
            result["coverage"].update(first=aggregate["first"], last=aggregate["last"])
            return result
    resolution = next((level for level in LEVELS if bucket_count(start, end, level) <= max_points), "year")
    buckets = read_exact(ddb, observation_table, rollup_table, station_id, resolution, start, end, budget)
    if len(buckets) > max_points:
        width = math.ceil(len(buckets) / max_points)
        buckets = [(buckets[index][0], buckets[min(index + width, len(buckets)) - 1][1],
                    merge_aggregates(bucket[2] for bucket in buckets[index:index + width]))
                   for index in range(0, len(buckets), width)]
        result["resolutionYears"] = width
    items = []
    for bucket_start, bucket_end, aggregate in buckets:
        items.append({"sk": "AGG#" + resolution + "#" + iso(bucket_start),
                      "display_ts_utc": iso(bucket_start), "bucketStart": iso(bucket_start),
                      "bucketEnd": iso(bucket_end), "count": aggregate["count"],
                      "weather": weather_values(aggregate), "stats": public_summary(aggregate)["metrics"]})
    aggregate = merge_aggregates(bucket[2] for bucket in buckets)
    result.update(resolution=resolution, items=items, count=len(items), totalMatched=aggregate["count"], summary=public_summary(aggregate))
    result["coverage"].update(first=aggregate["first"], last=aggregate["last"])
    return result
