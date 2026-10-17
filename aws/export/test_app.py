import copy
import csv
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

with patch("boto3.client"):
    spec = importlib.util.spec_from_file_location("export_app_test", Path(__file__).with_name("app.py"))
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


def error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "TestOperation")


class FakeDynamo:
    def __init__(self, job, rows):
        self.items = {job["pk"]: copy.deepcopy(job), "LOCK#garden": {"pk": "LOCK#garden", "jobId": job["jobId"], "expiresAt": 12000}}
        self.rows, self.queries = rows, 0
        self.page_size = 1000
        self.fail_checkpoint = False
        self.fail_ready = False
        self.on_query = None

    def get_item(self, **kwargs):
        key = app.decode(kwargs["Key"])["pk"]
        item = self.items.get(key)
        return {"Item": app.encode(copy.deepcopy(item))} if item else {}

    def check(self, item, kwargs):
        condition = kwargs.get("ConditionExpression", "")
        values = app.decode(kwargs.get("ExpressionAttributeValues", {}))
        valid = True
        if "version = :version" in condition:
            valid = valid and item.get("version") == values[":version"]
        if "#status IN (:queued, :running)" in condition:
            valid = valid and item.get("status") in ("queued", "running")
        elif "#status = :running" in condition:
            valid = valid and item.get("status") == "running"
        if "leaseUntil < :now" in condition:
            valid = valid and item.get("leaseUntil", 0) < values[":now"]
        if "leaseToken = :token" in condition:
            valid = valid and item.get("leaseToken") == values[":token"]
        if "updatedAt = :updated" in condition:
            valid = valid and item.get("updatedAt") == values[":updated"]
        if "jobId = :job" in condition:
            valid = valid and item.get("jobId") == values[":job"]
        if not valid:
            raise error("ConditionalCheckFailedException")

    def update_item(self, **kwargs):
        key = app.decode(kwargs["Key"])["pk"]
        item = self.items[key]
        self.check(item, kwargs)
        values = app.decode(kwargs.get("ExpressionAttributeValues", {}))
        if self.fail_ready and values.get(":ready") == "ready":
            self.fail_ready = False
            raise RuntimeError("transient_after_s3_completion")
        names = kwargs.get("ExpressionAttributeNames", {})
        for expression in kwargs["UpdateExpression"].removeprefix("SET ").split(","):
            left, right = [part.strip() for part in expression.split("=")]
            item[names.get(left, left)] = values[right]
        return {}

    def put_item(self, **kwargs):
        item = app.decode(kwargs["Item"])
        current = self.items.get(item["pk"], {})
        self.check(current, kwargs)
        if self.fail_checkpoint:
            self.fail_checkpoint = False
            raise RuntimeError("transient_before_checkpoint")
        self.items[item["pk"]] = copy.deepcopy(item)

    def query(self, **kwargs):
        self.last_query = kwargs
        self.queries += 1
        if self.on_query:
            self.on_query()
        cursor = kwargs.get("ExclusiveStartKey")
        offset = int(cursor["offset"]["N"]) if cursor else 0
        page = self.rows[offset:offset + self.page_size]
        result = {"Items": [app.encode(row) for row in page]}
        if offset + len(page) < len(self.rows):
            result["LastEvaluatedKey"] = {"offset": {"N": str(offset + len(page))}}
        return result


class FakeS3:
    def __init__(self):
        self.uploads, self.objects = {}, {}
        self.upload_number = 0
        self.parts_uploaded, self.aborted = [], []

    def create_multipart_upload(self, **kwargs):
        self.upload_number += 1
        key = str(self.upload_number)
        self.uploads[key] = {}
        return {"UploadId": key}

    def upload_part(self, **kwargs):
        upload = self.uploads.get(kwargs["UploadId"])
        if upload is None:
            raise error("NoSuchUpload")
        number = kwargs["PartNumber"]
        upload[number] = bytes(kwargs["Body"])
        self.parts_uploaded.append(number)
        return {"ETag": f"etag-{number}"}

    def complete_multipart_upload(self, **kwargs):
        upload = self.uploads.pop(kwargs["UploadId"], None)
        if upload is None:
            raise error("NoSuchUpload")
        self.objects[kwargs["Key"]] = b"".join(upload[part["PartNumber"]] for part in kwargs["MultipartUpload"]["Parts"])
        return {}

    def abort_multipart_upload(self, **kwargs):
        if kwargs["UploadId"] not in self.uploads:
            raise error("NoSuchUpload")
        del self.uploads[kwargs["UploadId"]]
        self.aborted.append(kwargs["UploadId"])

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])

    def get_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise error("404")
        return {}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)

    def generate_presigned_url(self, operation, **kwargs):
        return "https://private.example/download"


class FakeSQS:
    def __init__(self):
        self.messages, self.fail = [], False

    def send_message(self, **kwargs):
        if self.fail:
            self.fail = False
            raise RuntimeError("queue_send_transient_failure")
        self.messages.append(kwargs)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.job_id = "a" * 32
        self.job = dict(pk="JOB#" + self.job_id, jobId=self.job_id, stationId="garden", status="queued",
                        version=0, rowsExported=0, createdAt=10000, updatedAt=10000, expiresAt=96400,
                        fileName="readings.csv", outputKey="downloads/test.csv",
                        **{"from": "2026-01-01T00:00:00.000000Z", "to": "2026-12-31T23:59:59.000000Z"})
        self.rows = [{"pk": "STATION#garden", "sk": "OBS#" + str(index),
                      "display_ts_utc": f"2026-01-01T01:0{index}:00Z", "weather": {"air_temp_c": index}}
                     for index in range(3)]
        self.db, self.s3, self.queue = FakeDynamo(self.job, self.rows), FakeS3(), FakeSQS()
        self.patches = [patch.object(app, "ddb", self.db), patch.object(app, "s3", self.s3),
                        patch.object(app, "sqs", self.queue), patch.object(app.time, "time", return_value=10000),
                        patch.object(app, "SECRET", "secret")]
        for current in self.patches:
            current.start()
        self.addCleanup(lambda: [current.stop() for current in reversed(self.patches)])

    def current(self):
        return self.db.items[self.job["pk"]]

    def expire_lease(self):
        self.current()["leaseUntil"] = 0

    def csv_result(self):
        return list(csv.reader(io.StringIO(self.s3.objects[self.job["outputKey"]].decode())))

    def test_complete_and_duplicate_message_do_not_repeat_rows(self):
        app.process_job(self.job_id, None)
        app.process_job(self.job_id, None)
        self.assertEqual("ready", self.current()["status"])
        self.assertEqual(3, self.current()["rowsExported"])
        self.assertEqual(4, len(self.csv_result()))
        self.assertEqual(1, self.db.queries)
        self.assertIn("history_sort", self.db.last_query["ProjectionExpression"])

    def test_uncheckpointed_part_is_overwritten_on_retry(self):
        self.db.fail_checkpoint = True
        with self.assertRaisesRegex(RuntimeError, "before_checkpoint"):
            app.process_job(self.job_id, None)
        self.expire_lease()
        app.process_job(self.job_id, None)
        self.assertEqual([1, 1], self.s3.parts_uploaded)
        self.assertEqual(4, len(self.csv_result()))
        self.assertEqual(3, self.current()["rowsExported"])

    def test_final_checkpoint_resumes_after_completion_without_new_query(self):
        self.db.fail_ready = True
        with self.assertRaisesRegex(RuntimeError, "after_s3_completion"):
            app.process_job(self.job_id, None)
        app.process_job(self.job_id, None)
        self.assertEqual("ready", self.current()["status"])
        self.assertEqual(1, self.db.queries)
        self.assertEqual(4, len(self.csv_result()))

    def test_continuation_failure_resumes_checkpoint_without_repeating_rows(self):
        self.db.page_size, self.queue.fail = 1, True
        with patch.object(app, "MAX_PAGES", 1):
            with self.assertRaisesRegex(RuntimeError, "queue_send"):
                app.process_job(self.job_id, None)
            self.assertEqual(1, self.current()["rowsExported"])
            app.process_job(self.job_id, None)
            app.process_job(self.job_id, None)
        self.assertEqual(3, self.db.queries)
        self.assertEqual(4, len(self.csv_result()))
        self.assertEqual("ready", self.current()["status"])

    def test_active_lease_prevents_duplicate_work(self):
        self.current().update(status="running", leaseUntil=10120)
        with self.assertRaises(ClientError):
            app.process_job(self.job_id, None)
        self.assertEqual(0, self.db.queries)

    def test_cancel_during_read_never_resurrects_job(self):
        self.db.on_query = lambda: self.current().update(status="cancelled")
        app.process_job(self.job_id, None)
        self.assertEqual("cancelled", self.current()["status"])
        self.assertEqual(["1"], self.s3.aborted)
        self.assertNotIn(self.job["outputKey"], self.s3.objects)

    def test_cancel_finished_checkpoint_prevents_download(self):
        self.db.fail_ready = True
        with self.assertRaises(RuntimeError):
            app.process_job(self.job_id, None)
        self.current()["status"] = "cancelled"
        job = copy.deepcopy(self.current())
        state = json.loads(self.s3.objects[job["stateKey"]])
        app.finish_job(job, state)
        self.assertEqual("cancelled", self.current()["status"])
        self.assertNotIn(self.job["outputKey"], self.s3.objects)

    def test_stale_job_get_marks_failed_and_releases_slot(self):
        self.current().update(status="running", updatedAt=10000 - app.STALL_SECONDS - 1, leaseUntil=0)
        response = app.api_handler({"requestContext": {"http": {"method": "GET"}},
                                    "headers": {"x-weatherstation-key": "secret"},
                                    "rawPath": "/observations/exports/" + self.job_id}, None)
        body = json.loads(response["body"])
        self.assertEqual("failed", body["status"])
        self.assertEqual("export_stalled_retry", body["error"])
        self.assertEqual(0, self.db.items["LOCK#garden"]["expiresAt"])

    def test_stale_check_does_not_fail_active_lease(self):
        self.current().update(status="running", updatedAt=1, leaseUntil=10120)
        app.expire_stalled_job(copy.deepcopy(self.current()))
        self.assertEqual("running", self.current()["status"])

    def test_stale_check_is_fenced_by_progress_version(self):
        old = copy.deepcopy(self.current())
        old.update(updatedAt=1)
        self.current().update(version=1, updatedAt=9999)
        app.expire_stalled_job(old)
        self.assertEqual("queued", self.current()["status"])

    def test_each_invocation_limits_pages(self):
        self.db.page_size = 1
        with patch.object(app, "MAX_PAGES", 2):
            app.process_job(self.job_id, None)
        self.assertEqual(2, self.db.queries)
        self.assertEqual(2, self.current()["rowsExported"])
        self.assertEqual(1, len(self.queue.messages))

    def test_multipart_resume_keeps_one_header_and_all_rows(self):
        self.db.page_size = 1
        with patch.object(app, "MAX_PAGES", 1), patch.object(app, "PART_MIN", 1):
            for _ in range(3):
                app.process_job(self.job_id, None)
        self.assertEqual([1, 2, 3], self.s3.parts_uploaded)
        result = self.csv_result()
        self.assertEqual(4, len(result))
        self.assertEqual(["0", "1", "2"], [row[list(app.COLUMNS).index("air_temp_c")] for row in result[1:]])

    def test_checkpoint_is_fenced_when_lease_owner_changes(self):
        self.db.on_query = lambda: self.current().update(leaseToken="new-owner")
        with self.assertRaises(ClientError):
            app.process_job(self.job_id, None)
        self.assertEqual(0, self.current()["rowsExported"])
        self.assertEqual(0, self.current()["version"])

    def test_csv_uses_receipt_time_for_suspect_clock_and_preserves_numeric_negatives(self):
        row = app.encode({"source_ts_utc": "2026-01-01T01:00:00Z", "received_at_utc": "2026-01-01T12:00:00Z",
                          "ingested_at_utc": "2026-01-02T12:00:00Z", "weather": {"air_temp_c": -5}})
        result = list(csv.reader(io.StringIO(app.csv_rows([row], "garden").decode())))[0]
        self.assertEqual("2026-01-01T12:00:00.000000Z", result[list(app.COLUMNS).index("display_ts_utc")])
        self.assertEqual("true", result[list(app.COLUMNS).index("source_time_suspect")])
        self.assertEqual("-5", result[list(app.COLUMNS).index("air_temp_c")])

    def test_numeric_legacy_utc_columns_are_iso_and_match_index(self):
        canonical = app.normalize_time("1773794743")
        row = app.encode({"source_ts_utc": Decimal("1773794743"), "received_at_utc": "1773798343000",
                          "ingested_at_utc": Decimal("1773801943000000"), "display_ts_utc": "1773794743",
                          "history_sort": "DISPLAY#" + canonical + "#OBS#legacy", "weather": {}})
        result = list(csv.reader(io.StringIO(app.csv_rows([row], "garden").decode())))[0]
        fields = dict(zip(app.COLUMNS, result))
        self.assertEqual(canonical, fields["source_ts_utc"])
        self.assertEqual(canonical, fields["display_ts_utc"])
        self.assertEqual(app.normalize_time("1773798343"), fields["received_at_utc"])
        self.assertEqual(app.normalize_time("1773801943"), fields["ingested_at_utc"])
        self.assertEqual("true", fields["source_time_suspect"])

    def test_epoch_normalization_accepts_all_legacy_units_without_precision_loss(self):
        expected = "2001-09-09T01:46:40.123456Z"
        for value in ("1000000000.123456", Decimal("1000000000123.456"), "1000000000123456"):
            with self.subTest(value=value):
                self.assertEqual(expected, app.normalize_time(value))

    def test_empty_export_still_has_csv_header(self):
        self.db.rows = []
        app.process_job(self.job_id, None)
        self.assertEqual(1, len(self.csv_result()))
        self.assertEqual(list(app.COLUMNS), self.csv_result()[0])

    def test_worker_reports_retry_and_ignores_terminal_jobs(self):
        self.current().update(status="running", leaseUntil=10120)
        event = {"Records": [{"messageId": "msg", "body": json.dumps({"jobId": self.job_id})}]}
        self.assertEqual({"batchItemFailures": [{"itemIdentifier": "msg"}]}, app.worker_handler(event, None))
        self.current()["status"] = "cancelled"
        self.assertEqual({"batchItemFailures": []}, app.worker_handler(event, None))


if __name__ == "__main__":
    unittest.main()
