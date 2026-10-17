"""Queued, resumable CSV exports; chart requests never perform this work."""
import base64
import csv
import hmac
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError
from botocore.config import Config

CLIENT_CONFIG = Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 1})
ddb = boto3.client("dynamodb", config=CLIENT_CONFIG)
s3 = boto3.client("s3", config=CLIENT_CONFIG)
sqs = boto3.client("sqs", config=CLIENT_CONFIG)
serializer, deserializer = TypeSerializer(), TypeDeserializer()
TABLE = os.environ.get("TABLE_NAME", "WeatherObservations")
JOBS = os.environ.get("EXPORT_JOBS_TABLE", "")
BUCKET = os.environ.get("EXPORT_BUCKET", "")
QUEUE = os.environ.get("EXPORT_QUEUE_URL", "")
INDEX = os.environ.get("HISTORY_INDEX_NAME", "ObservationDisplayTimeIndex")
SECRET = os.environ.get("API_SHARED_SECRET", "")
ORIGIN = os.environ.get("ALLOWED_CORS_ORIGIN", "*")
PART_TARGET = 8 * 1024 * 1024
PART_MIN = 5 * 1024 * 1024
MAX_PAGES = 10
LEASE_SECONDS = 120
STALL_SECONDS = int(os.environ.get("EXPORT_STALL_SECONDS", "1800"))
FIELDS = (
    "air_temp_c", "relative_humidity_pct", "station_pressure_hpa", "wind_avg_ms",
    "wind_lull_ms", "wind_gust_ms", "wind_dir_deg", "rain_interval_mm",
    "solar_radiation_wm2", "illuminance_lux", "uv_index", "lightning_strike_count",
    "lightning_avg_distance_km", "battery_voltage_v", "report_interval_min",
)
COLUMNS = ("station_id", "display_ts_utc", "source_ts_utc", "received_at_utc", "ingested_at_utc", "source_time_suspect") + FIELDS


def encode(item):
    return {key: serializer.serialize(value) for key, value in item.items()}


def decode(item):
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def response(status, body):
    return {"statusCode": status, "headers": {
        "content-type": "application/json", "cache-control": "no-store",
        "access-control-allow-origin": ORIGIN,
        "access-control-allow-headers": "content-type,x-weatherstation-key",
        "access-control-allow-methods": "GET,POST,DELETE,OPTIONS",
    }, "body": json.dumps(body, default=lambda value: int(value) if isinstance(value, Decimal) else str(value))}


def normalize_time(value):
    raw = str(value).strip()
    if isinstance(value, bool):
        raise ValueError("invalid_timestamp")
    if isinstance(value, (int, Decimal, float)) or re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        numeric = Decimal(raw)
        if not numeric.is_finite() or numeric < 0:
            raise ValueError("invalid_epoch_timestamp")
        if abs(numeric) >= Decimal("100000000000000"):
            numeric /= Decimal("1000000")
        elif abs(numeric) >= Decimal("100000000000"):
            numeric /= Decimal("1000")
        try:
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(numeric * 1000000))
        except (OverflowError, ValueError):
            raise ValueError("epoch_timestamp_out_of_range") from None
    else:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone_required")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_column(value):
    if value is None or value == "":
        return ""
    try:
        return normalize_time(value)
    except (ArithmeticError, TypeError, ValueError):
        return ""


def get_job(job_id):
    return decode(ddb.get_item(TableName=JOBS, Key=encode({"pk": "JOB#" + job_id}), ConsistentRead=True).get("Item", {}))


def release_slot(job):
    try:
        ddb.update_item(TableName=JOBS, Key=encode({"pk": "LOCK#" + job["stationId"]}),
            UpdateExpression="SET expiresAt = :expired", ConditionExpression="jobId = :job",
            ExpressionAttributeValues=encode({":expired": 0, ":job": job["jobId"]}))
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def abort_upload(job):
    if job.get("uploadId"):
        try:
            s3.abort_multipart_upload(Bucket=BUCKET, Key=job["outputKey"], UploadId=job["uploadId"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchUpload":
                raise


def fail_job(job, error, require_stale=False):
    """Fail only the unchanged active job, never overwrite a concurrent result."""
    now = int(time.time())
    condition = "version = :version AND #status IN (:queued, :running)"
    values = {":version": job["version"], ":queued": "queued", ":running": "running",
              ":failed": "failed", ":error": error, ":now": now, ":zero": 0}
    if require_stale:
        condition += " AND updatedAt = :updated AND (attribute_not_exists(leaseUntil) OR leaseUntil < :now)"
        values[":updated"] = job["updatedAt"]
    try:
        ddb.update_item(TableName=JOBS, Key=encode({"pk": job["pk"]}),
            UpdateExpression="SET #status = :failed, #error = :error, updatedAt = :now, leaseUntil = :zero",
            ConditionExpression=condition, ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues=encode(values))
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return get_job(job["jobId"])
    job.update(status="failed", error=error, updatedAt=now, leaseUntil=0)
    try:
        abort_upload(job)
    finally:
        release_slot(job)
    return job


def expire_stalled_job(job):
    now = int(time.time())
    if job and job.get("status") in ("queued", "running") and job.get("leaseUntil", 0) < now:
        if job.get("expiresAt", 0) <= now:
            return fail_job(job, "export_expired", require_stale=True)
        if now - job.get("updatedAt", job.get("createdAt", now)) > STALL_SECONDS:
            return fail_job(job, "export_stalled_retry", require_stale=True)
    return job


def public_job(job):
    result = {"ok": True, **{key: job[key] for key in ("jobId", "status", "rowsExported", "fileName", "expiresAt")}}
    if job.get("error"):
        result["error"] = job["error"]
    if job["status"] == "ready":
        result["downloadUrl"] = s3.generate_presigned_url("get_object", Params={
            "Bucket": BUCKET, "Key": job["outputKey"],
            "ResponseContentDisposition": f'attachment; filename="{job["fileName"]}"',
        }, ExpiresIn=900)
    return result


def create_job(body):
    station = str(body.get("stationId", ""))
    if not re.fullmatch(r"[A-Za-z0-9_!#.-]{1,128}", station):
        return response(400, {"ok": False, "error": "invalid_stationId"})
    try:
        start, end = normalize_time(body["from"]), normalize_time(body["to"])
        if start >= end:
            raise ValueError("invalid_range")
    except (KeyError, ValueError, TypeError):
        return response(400, {"ok": False, "error": "invalid_time_range"})
    latest = ddb.get_item(TableName=TABLE, Key=encode({"pk": "STATION#" + station, "sk": "LATEST"}), ProjectionExpression="pk").get("Item")
    if not latest:
        return response(404, {"ok": False, "error": "station_not_found"})
    now, job_id = int(time.time()), uuid.uuid4().hex
    job = dict(pk="JOB#" + job_id, jobId=job_id, stationId=station, status="queued",
        rowsExported=0, version=0, createdAt=now, updatedAt=now, expiresAt=now + 86400,
        fileName=f"weather-readings-{start[:10]}-to-{end[:10]}-UTC-SI.csv",
        outputKey=f"downloads/{job_id}.csv", **{"from": start, "to": end})
    try:
        ddb.transact_write_items(TransactItems=[
            {"Put": {"TableName": JOBS, "Item": encode(job), "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": JOBS, "Item": encode({"pk": "LOCK#" + station, "jobId": job_id, "expiresAt": now + STALL_SECONDS + LEASE_SECONDS}),
                "ConditionExpression": "attribute_not_exists(pk) OR expiresAt < :now", "ExpressionAttributeValues": encode({":now": now})}},
        ])
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        lock = decode(ddb.get_item(TableName=JOBS, Key=encode({"pk": "LOCK#" + station}), ConsistentRead=True).get("Item", {}))
        existing = expire_stalled_job(get_job(lock.get("jobId", ""))) if lock.get("jobId") else {}
        if existing and existing.get("status") in ("queued", "running") and existing.get("from") == start and existing.get("to") == end:
            return response(202, public_job(existing))
        return response(429, {"ok": False, "error": "export_already_running", "message": "An export is already running for this station. Try again when it finishes."})
    try:
        sqs.send_message(QueueUrl=QUEUE, MessageBody=json.dumps({"jobId": job_id}))
    except Exception:
        fail_job(job, "export_queue_unavailable")
        return response(503, {"ok": False, "error": "export_queue_unavailable"})
    return response(202, public_job(job))


def api_handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "").upper()
    if method == "OPTIONS":
        return response(200, {"ok": True})
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    if not SECRET or not hmac.compare_digest(headers.get("x-weatherstation-key", ""), SECRET):
        return response(401, {"ok": False, "error": "unauthorized"})
    path = event.get("rawPath", "").rstrip("/")
    if method == "POST" and path == "/observations/exports":
        try:
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode()
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("invalid_body")
        except (ValueError, TypeError):
            return response(400, {"ok": False, "error": "invalid_json"})
        return create_job(body)
    job_id = path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or method not in ("GET", "DELETE"):
        return response(404, {"ok": False, "error": "route_not_found"})
    job = expire_stalled_job(get_job(job_id))
    if not job:
        return response(404, {"ok": False, "error": "export_not_found"})
    if job["expiresAt"] < int(time.time()):
        return response(410, {"ok": False, "error": "export_expired"})
    if method == "DELETE" and job["status"] in ("queued", "running"):
        try:
            ddb.update_item(TableName=JOBS, Key=encode({"pk": job["pk"]}),
                UpdateExpression="SET #status = :cancelled, updatedAt = :now", ConditionExpression="#status IN (:queued, :running)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=encode({":cancelled": "cancelled", ":queued": "queued", ":running": "running", ":now": int(time.time())}))
            job["status"] = "cancelled"
            try:
                abort_upload(job)
            finally:
                release_slot(job)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            job = get_job(job_id)
    if job["status"] in ("ready", "failed", "cancelled"):
        release_slot(job)
    return response(200, public_job(job))


def csv_rows(items, station, header=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if header:
        writer.writerow(COLUMNS)
    for raw in items:
        item = decode(raw)
        source = utc_column(item.get("source_ts_utc") or item.get("source_ts_sort_utc"))
        received = utc_column(item.get("received_at_utc"))
        suspect = bool(item.get("source_time_suspect"))
        if source and received:
            try:
                suspect = suspect or abs((datetime.fromisoformat(received.replace("Z", "+00:00")) - datetime.fromisoformat(source.replace("Z", "+00:00"))).total_seconds()) > 600
            except ValueError:
                pass
        display = received if suspect and received else utc_column(item.get("display_ts_utc")) or source or received
        indexed = item.get("history_sort")
        if isinstance(indexed, str) and indexed.startswith("DISPLAY#"):
            display = utc_column(indexed.split("#", 2)[1]) or display
        ingested = utc_column(item.get("ingested_at_utc") or item.get("updated_at_utc"))
        weather = item.get("weather") or {}
        values = []
        for name in FIELDS:
            value = weather.get(name, weather.get("rain_mm") if name == "rain_interval_mm" else None)
            # Export known numeric measurements only; preserve negatives as numbers.
            try:
                number = Decimal(str(value))
                values.append(str(number) if number.is_finite() else "")
            except Exception:
                values.append("")
        writer.writerow([station, display or ingested, source, received, ingested, str(suspect).lower(), *values])
    return output.getvalue().encode("utf-8")


def process_job(job_id, context):
    job = expire_stalled_job(get_job(job_id))
    if not job:
        return
    if job["status"] == "ready":
        release_slot(job)
        return
    if job["status"] in ("cancelled", "failed"):
        try:
            abort_upload(job)
        finally:
            release_slot(job)
        return
    now = int(time.time())
    if job["expiresAt"] <= now:
        fail_job(job, "export_expired")
        return
    version = int(job["version"])
    lease_token = uuid.uuid4().hex
    ddb.update_item(TableName=JOBS, Key=encode({"pk": job["pk"]}),
        UpdateExpression="SET #status = :running, leaseUntil = :lease, leaseToken = :token",
        ConditionExpression="version = :version AND #status IN (:queued, :running) AND (attribute_not_exists(leaseUntil) OR leaseUntil < :now)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=encode({":running": "running", ":queued": "queued", ":version": version, ":lease": now + LEASE_SECONDS, ":now": now, ":token": lease_token}))
    job.update(status="running", leaseUntil=now + LEASE_SECONDS, leaseToken=lease_token)
    if job.get("stateKey"):
        state = json.loads(s3.get_object(Bucket=BUCKET, Key=job["stateKey"])["Body"].read())
    else:
        state = {"cursor": None, "parts": [], "buffer": base64.b64encode(csv_rows([], job["stationId"], header=True)).decode()}
    if state.get("finished"):
        finish_job(job, state)
        return
    if not job.get("uploadId"):
        job["uploadId"] = s3.create_multipart_upload(Bucket=BUCKET, Key=job["outputKey"], ContentType="text/csv", ContentDisposition=f'attachment; filename="{job["fileName"]}"')["UploadId"]
        try:
            ddb.update_item(TableName=JOBS, Key=encode({"pk": job["pk"]}), UpdateExpression="SET uploadId = :id",
                ConditionExpression="#status = :running AND leaseToken = :token",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=encode({":id": job["uploadId"], ":running": "running", ":token": lease_token}))
        except Exception:
            abort_upload(job)
            raise
    buffer = bytearray(base64.b64decode(state["buffer"]))
    finished, pages = False, 0
    seconds_left = context.get_remaining_time_in_millis() / 1000 if context and hasattr(context, "get_remaining_time_in_millis") else 60
    deadline = time.monotonic() + max(0, min(25, seconds_left - 25))
    while pages < MAX_PAGES and len(buffer) < PART_TARGET and time.monotonic() < deadline:
        request = dict(TableName=TABLE, IndexName=INDEX,
            KeyConditionExpression="pk = :pk AND history_sort BETWEEN :start AND :end",
            ExpressionAttributeValues=encode({":pk": "STATION#" + job["stationId"], ":start": "DISPLAY#" + job["from"], ":end": "DISPLAY#" + job["to"] + "~"}),
            ProjectionExpression="pk, sk, history_sort, display_ts_utc, source_ts_utc, source_ts_sort_utc, received_at_utc, ingested_at_utc, updated_at_utc, source_time_suspect, weather",
            ScanIndexForward=True, Limit=1000)
        if state["cursor"]:
            request["ExclusiveStartKey"] = state["cursor"]
        page = ddb.query(**request)
        rows = page.get("Items", [])
        buffer.extend(csv_rows(rows, job["stationId"]))
        job["rowsExported"] += len(rows)
        state["cursor"] = page.get("LastEvaluatedKey")
        pages += 1
        if not state["cursor"]:
            finished = True
            break
    if len(buffer) >= PART_MIN or finished:
        part_number = len(state["parts"]) + 1
        if part_number > 10000:
            fail_job(job, "export_too_large_split_range")
            return
        part = s3.upload_part(Bucket=BUCKET, Key=job["outputKey"], UploadId=job["uploadId"], PartNumber=part_number, Body=bytes(buffer))
        state["parts"].append({"PartNumber": part_number, "ETag": part["ETag"]})
        buffer.clear()
    state["buffer"] = base64.b64encode(buffer).decode()
    # Save the final manifest before completion so retries can finish an already
    # completed multipart upload without duplicating records or losing progress.
    state["finished"] = finished
    job.update(version=version + 1, updatedAt=int(time.time()), leaseUntil=0,
        stateKey=f"state/{job_id}/{version + 1}-{lease_token}.json")
    s3.put_object(Bucket=BUCKET, Key=job["stateKey"], Body=json.dumps(state).encode(), ContentType="application/json")
    try:
        ddb.put_item(TableName=JOBS, Item=encode(job), ConditionExpression="version = :version AND #status = :running AND leaseToken = :token", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues=encode({":version": version, ":running": "running", ":token": lease_token}))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException" and get_job(job_id).get("status") in ("cancelled", "failed"):
            abort_upload(job)
            return
        raise
    if finished:
        finish_job(job, state)
    else:
        ddb.update_item(TableName=JOBS, Key=encode({"pk": "LOCK#" + job["stationId"]}), UpdateExpression="SET expiresAt = :expiry", ConditionExpression="jobId = :job", ExpressionAttributeValues=encode({":expiry": int(time.time()) + STALL_SECONDS + LEASE_SECONDS, ":job": job_id}))
        sqs.send_message(QueueUrl=QUEUE, MessageBody=json.dumps({"jobId": job_id}))


def finish_job(job, state):
    try:
        s3.complete_multipart_upload(Bucket=BUCKET, Key=job["outputKey"], UploadId=job["uploadId"], MultipartUpload={"Parts": state["parts"]})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchUpload":
            raise
        s3.head_object(Bucket=BUCKET, Key=job["outputKey"])
    try:
        ddb.update_item(TableName=JOBS, Key=encode({"pk": job["pk"]}), UpdateExpression="SET #status = :ready, leaseUntil = :zero, updatedAt = :now", ConditionExpression="#status = :running AND version = :version", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues=encode({":ready": "ready", ":running": "running", ":zero": 0, ":now": int(time.time()), ":version": job["version"]}))
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        current = get_job(job["jobId"])
        if current.get("status") in ("cancelled", "failed"):
            s3.delete_object(Bucket=BUCKET, Key=job["outputKey"])
        elif current.get("status") != "ready":
            raise
    release_slot(job)


def worker_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        job_id = None
        try:
            job_id = json.loads(record["body"])["jobId"]
            process_job(job_id, context)
        except Exception as exc:
            print(json.dumps({"event": "export_retry", "type": type(exc).__name__, "jobId": locals().get("job_id")}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
