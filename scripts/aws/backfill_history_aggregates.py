#!/usr/bin/env python3
"""Build a separate summary table without modifying any raw observations.

Enable the observation stream before running this, keep its consumer disabled
until the backfill is complete, then start the consumer at TRIM_HORIZON. This
captures uploads during the scan; idempotent hour rebuilding reconciles them.
The observation scan must finish within the stream's 24-hour retention period.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "aws" / "rollup"))
from aggregate_history import (
    UTC, LEVELS, decode_item, effective_time, encode_item, floor_bucket,
    iso, merge_aggregates, rollup_row, summarize_observations,
)


def build_rows(observations):
    """Aggregate a streaming input in memory proportional to occupied hours."""
    hourly, scanned = {}, 0
    for item in observations:
        if not str(item.get("sk", "")).startswith("OBS#") or not str(item.get("pk", "")).startswith("STATION#"):
            continue
        station = item["pk"][len("STATION#"):]
        key = (station, floor_bucket(effective_time(item), "hour"))
        hourly[key] = merge_aggregates((hourly.get(key), summarize_observations([item])))
        scanned += 1
    updated = iso(datetime.now(UTC))
    rows, station_totals, children = [], {}, hourly
    for resolution in LEVELS:
        if resolution != "hour":
            grouped = {}
            for (station, start), aggregate in children.items():
                key = (station, floor_bucket(start, resolution))
                grouped[key] = merge_aggregates((grouped.get(key), aggregate))
            children = grouped
        for (station, start), aggregate in sorted(children.items()):
            rows.append(rollup_row(station, resolution, start, aggregate, updated))
            if resolution == "year":
                station_totals[station] = merge_aggregates((station_totals.get(station), aggregate))
    for station, aggregate in sorted(station_totals.items()):
        rows.append({"pk": f"STATION#{station}", "sk": "META", "availableFrom": aggregate["first"],
                     "availableTo": aggregate["last"], "updatedAt": updated, "schemaVersion": 1})
    return rows, scanned


def scan_observations(ddb, table, station=None):
    args = {"TableName": table, "ConsistentRead": True,
            "FilterExpression": "begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {":prefix": {"S": "OBS#"}}, "Limit": 1000}
    if station:
        args["FilterExpression"] += " AND pk = :station"
        args["ExpressionAttributeValues"][":station"] = {"S": f"STATION#{station}"}
    while True:
        page = ddb.scan(**args)
        for raw in page.get("Items", []):
            yield decode_item(raw)
        if not page.get("LastEvaluatedKey"):
            break
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def write_rows(ddb, table, rows):
    for offset in range(0, len(rows), 25):
        pending = {table: [{"PutRequest": {"Item": encode_item(row)}} for row in rows[offset:offset + 25]]}
        for retry in range(10):
            result = ddb.batch_write_item(RequestItems=pending)
            pending = result.get("UnprocessedItems") or {}
            if not any(pending.values()):
                break
            time.sleep(min(0.2 * 2 ** retry, 10))
        else:
            raise RuntimeError("aggregate_backfill_write_retries_exhausted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="Existing raw observation table")
    parser.add_argument("--rollup-table", required=True, help="Separate aggregate table")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile")
    parser.add_argument("--station")
    parser.add_argument("--apply", action="store_true", help="Write the computed rows; otherwise only report")
    args = parser.parse_args()
    if args.table == args.rollup_table:
        parser.error("Observation and rollup tables must be different")
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ddb = session.client("dynamodb")
    rows, count = build_rows(scan_observations(ddb, args.table, args.station))
    report = {"apply": args.apply, "observations": count, "rows": len(rows),
              "buckets": {level: sum(row.get("resolution") == level for row in rows) for level in LEVELS},
              "stations": sum(row["sk"] == "META" for row in rows)}
    print(json.dumps(report), flush=True)
    if args.apply:
        write_rows(ddb, args.rollup_table, rows)
        print(json.dumps({"ok": True, "rowsWritten": len(rows)}), flush=True)


if __name__ == "__main__":
    main()
