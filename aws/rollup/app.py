"""Idempotent stream consumer: rebuild affected buckets from their children."""
import os
from datetime import datetime

from aggregate_history import (
    UTC, LEVELS, QueryBudget, decode_item, effective_time, encode_item,
    fetch_raw, fetch_rollups, floor_bucket, iso, merge_aggregates, next_bucket,
    rollup_row, summarize_observations,
)


def rebuild_hours(ddb, observation_table, rollup_table, pending):
    """pending maps station IDs to hour -> triggering observation sort keys.

    The event source mapping must have reserved concurrency one. Rebuilding
    parents after all changed children makes both stream retries and late
    observations safe without per-event counters or deduplication tables.
    """
    updated_at = iso(datetime.now(UTC))
    written = 0
    for station_id, hours in pending.items():
        changed = set(hours)
        observed = []
        for start in sorted(changed):
            rows, _ = fetch_raw(ddb, observation_table, station_id, start, next_bucket(start, "hour"), QueryBudget(seconds=90))
            visible_keys = {row["sk"] for row in rows}
            if not hours[start].issubset(visible_keys):
                # The observation stream may lead the eventually consistent GSI.
                # Retry the batch instead of publishing an incomplete hour.
                raise RuntimeError("history_display_index_not_caught_up")
            aggregate = summarize_observations(rows)
            ddb.put_item(TableName=rollup_table, Item=encode_item(rollup_row(station_id, "hour", start, aggregate, updated_at)))
            observed.append(aggregate)
            written += 1
        for level_index in range(1, len(LEVELS)):
            resolution, child = LEVELS[level_index], LEVELS[level_index - 1]
            changed = {floor_bucket(start, resolution) for start in changed}
            for start in sorted(changed):
                children = fetch_rollups(ddb, rollup_table, station_id, child, start, next_bucket(start, resolution), QueryBudget(seconds=90))
                aggregate = merge_aggregates(row["aggregate"] for row in children)
                ddb.put_item(TableName=rollup_table, Item=encode_item(rollup_row(station_id, resolution, start, aggregate, updated_at)))
                written += 1
        key = {"pk": f"STATION#{station_id}", "sk": "META"}
        old = ddb.get_item(TableName=rollup_table, Key=encode_item(key), ConsistentRead=True).get("Item")
        meta = decode_item(old) if old else dict(key)
        aggregate = merge_aggregates(observed)
        if aggregate["count"]:
            meta["availableFrom"] = min(meta.get("availableFrom") or aggregate["first"], aggregate["first"])
            meta["availableTo"] = max(meta.get("availableTo") or aggregate["last"], aggregate["last"])
        meta.update(updatedAt=updated_at, schemaVersion=1)
        ddb.put_item(TableName=rollup_table, Item=encode_item(meta))
    return written


def lambda_handler(event, context):
    import boto3
    pending = {}
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        raw = record.get("dynamodb", {}).get("NewImage")
        if not raw:
            continue
        item = decode_item(raw)
        if not str(item.get("sk", "")).startswith("OBS#") or not str(item.get("pk", "")).startswith("STATION#"):
            continue
        station_id = item["pk"][len("STATION#"):]
        hour = floor_bucket(effective_time(item), "hour")
        pending.setdefault(station_id, {}).setdefault(hour, set()).add(item["sk"])
    written = rebuild_hours(boto3.client("dynamodb"), os.environ["TABLE_NAME"], os.environ["ROLLUP_TABLE_NAME"], pending)
    return {"ok": True, "bucketsWritten": written, "stations": len(pending)}
