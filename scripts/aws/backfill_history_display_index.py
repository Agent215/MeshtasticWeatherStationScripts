"""Add display-time index keys without replacing or deleting observation data.

Dry-run by default. Deploy the updated ingest function before --apply, then run
again without --apply to verify missing=0 and mismatched=0 before enabling reads.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def load_reader(table, region):
    os.environ["TABLE_NAME"] = table
    os.environ["AWS_DEFAULT_REGION"] = region
    path = Path(__file__).resolve().parents[2] / "aws" / "read" / "app.py"
    spec = importlib.util.spec_from_file_location("history_reader", path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def add_index_key(ddb, table, item, history_sort):
    try:
        ddb.update_item(
            TableName=table,
            Key={key: item[key] for key in ("pk", "sk")},
            UpdateExpression="SET history_sort = :history_sort",
            ConditionExpression="attribute_exists(pk) AND attribute_exists(sk) AND attribute_not_exists(history_sort)",
            ExpressionAttributeValues={":history_sort": {"S": history_sort}},
        )
        return "updated"
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # A concurrent worker may already have populated this immutable row.
        current = ddb.get_item(
            TableName=table, Key={key: item[key] for key in ("pk", "sk")},
            ConsistentRead=True, ProjectionExpression="history_sort",
        ).get("Item", {})
        if current.get("history_sort", {}).get("S") != history_sort:
            raise RuntimeError("Observation changed or was removed during backfill") from exc
        return "already_indexed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("workers must be between 1 and 32")
    reader = load_reader(args.table, args.region)
    ddb = boto3.client("dynamodb", region_name=args.region, config=Config(
        max_pool_connections=args.workers,
        retries={"mode": "adaptive", "max_attempts": 10},
    ))
    totals = dict(observations=0, missing=0, mismatched=0, updated=0, already_indexed=0)
    scan = dict(
        TableName=args.table, Limit=500, ConsistentRead=True,
        FilterExpression="begins_with(sk, :observation)",
        ExpressionAttributeValues={":observation": {"S": "OBS#"}},
        ProjectionExpression="pk, sk, source_ts_utc, source_ts_sort_utc, received_at_utc, ingested_at_utc, updated_at_utc, display_ts_utc, source_time_suspect, source_clock_skew_seconds, history_sort",
    )
    pages = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        while True:
            page = ddb.scan(**scan)
            pending = []
            for raw in page.get("Items", []):
                item = reader.normalize_item(raw)
                expected = reader.build_display_history_sort(item)
                totals["observations"] += 1
                existing = item.get("history_sort")
                if existing is not None:
                    if existing != expected:
                        totals["mismatched"] += 1
                    continue
                totals["missing"] += 1
                if args.apply:
                    pending.append(executor.submit(add_index_key, ddb, args.table, raw, expected))
            for future in pending:
                totals[future.result()] += 1
            pages += 1
            if pages % 10 == 0:
                print(json.dumps({"progress": totals}), flush=True)
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                break
            scan["ExclusiveStartKey"] = cursor
    print(json.dumps({"complete": True, "applied": args.apply, **totals}), flush=True)
    if totals["mismatched"]:
        raise SystemExit("Existing index keys do not match display timestamps; investigate before cutover")
    if not args.apply and totals["missing"]:
        raise SystemExit("Backfill required before cutover")


if __name__ == "__main__":
    main()
