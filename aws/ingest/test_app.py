import copy
import importlib.util
import json
import os
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


class FakeClientError(Exception):
    def __init__(self, error_response, operation_name):
        super().__init__(error_response.get("Error", {}).get("Message", "DynamoDB error"))
        self.response = error_response
        self.operation_name = operation_name


class FakeTypeSerializer:
    def serialize(self, value):
        if value is None:
            return {"NULL": True}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, str):
            return {"S": value}
        if isinstance(value, (int, Decimal)):
            return {"N": str(value)}
        if isinstance(value, dict):
            return {"M": {key: self.serialize(child) for key, child in value.items()}}
        if isinstance(value, list):
            return {"L": [self.serialize(child) for child in value]}
        raise TypeError(f"Unsupported fake DynamoDB value: {type(value).__name__}")


class FakeTypeDeserializer:
    def deserialize(self, value):
        if "NULL" in value:
            return None
        if "BOOL" in value:
            return value["BOOL"]
        if "S" in value:
            return value["S"]
        if "N" in value:
            return Decimal(value["N"])
        if "M" in value:
            return {key: self.deserialize(child) for key, child in value["M"].items()}
        if "L" in value:
            return [self.deserialize(child) for child in value["L"]]
        raise TypeError(f"Unsupported fake DynamoDB attribute: {value!r}")


class FakeDynamoDB:
    def __init__(self):
        self.items = {}
        self.put_calls = []

    @staticmethod
    def _string(attribute):
        return attribute["S"]

    def _key_from_item(self, item):
        return self._string(item["pk"]), self._string(item["sk"])

    def _conditional_failure(self, operation_name):
        raise FakeClientError(
            {
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "The conditional request failed",
                }
            },
            operation_name,
        )

    def reset(self):
        self.items.clear()
        self.put_calls.clear()

    def get_item(self, **kwargs):
        key = (
            self._string(kwargs["Key"]["pk"]),
            self._string(kwargs["Key"]["sk"]),
        )
        item = self.items.get(key)
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, **kwargs):
        self.put_calls.append(copy.deepcopy(kwargs))
        item = copy.deepcopy(kwargs["Item"])
        key = self._key_from_item(item)
        current = self.items.get(key)
        condition = kwargs.get("ConditionExpression", "")

        if "attribute_not_exists(pk) AND attribute_not_exists(sk)" in condition:
            if current is not None:
                self._conditional_failure("PutItem")
        elif key[1] == "LATEST" and current is not None:
            incoming_received = kwargs["ExpressionAttributeValues"][":incoming_received_at"]["S"]
            incoming_ingested_attribute = kwargs["ExpressionAttributeValues"].get(
                ":incoming_ingested_at"
            )
            incoming_ingested = (
                incoming_ingested_attribute["S"]
                if incoming_ingested_attribute is not None
                else None
            )
            current_received = current.get("received_at_utc", {}).get("S")
            current_ingested = current.get("ingested_at_utc", {}).get("S")

            should_advance = (
                current_received is None
                or current_received < incoming_received
                or (
                    current_received == incoming_received
                    and incoming_ingested is not None
                    and (current_ingested is None or current_ingested < incoming_ingested)
                )
            )
            if not should_advance:
                self._conditional_failure("PutItem")

        self.items[key] = item
        return {}


fake_ddb = FakeDynamoDB()
deserializer = FakeTypeDeserializer()


def load_app_module():
    os.environ.setdefault("TABLE_NAME", "WeatherObservationsTest")
    os.environ.setdefault("API_SHARED_SECRET", "test-weather-secret")

    boto3_module = types.ModuleType("boto3")
    boto3_module.client = lambda service: fake_ddb

    boto3_dynamodb_module = types.ModuleType("boto3.dynamodb")
    boto3_types_module = types.ModuleType("boto3.dynamodb.types")
    boto3_types_module.TypeSerializer = FakeTypeSerializer
    boto3_types_module.TypeDeserializer = FakeTypeDeserializer

    botocore_module = types.ModuleType("botocore")
    botocore_exceptions_module = types.ModuleType("botocore.exceptions")
    botocore_exceptions_module.ClientError = FakeClientError

    sys.modules["boto3"] = boto3_module
    sys.modules["boto3.dynamodb"] = boto3_dynamodb_module
    sys.modules["boto3.dynamodb.types"] = boto3_types_module
    sys.modules["botocore"] = botocore_module
    sys.modules["botocore.exceptions"] = botocore_exceptions_module

    module_path = Path(__file__).with_name("app.py")
    spec = importlib.util.spec_from_file_location("weather_ingest_app", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load_app_module()


class ObservationIngestTests(unittest.TestCase):
    station_id = "!5c32d4d9"

    def setUp(self):
        fake_ddb.reset()

    def post_observation(
        self,
        *,
        msg_id,
        source_ts,
        received_at,
        ingested_at,
        temperature=20,
    ):
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": "/observations",
            "headers": {"x-weatherstation-key": "test-weather-secret"},
            "body": json.dumps(
                {
                    "payload": {
                        "source_node_id": self.station_id,
                        "source_name": "garden",
                        "msg_id": msg_id,
                        "source_ts_utc": source_ts,
                        "received_at_utc": received_at,
                        "weather": {"air_temp_c": temperature, "timestamp": source_ts},
                    }
                }
            ),
        }
        with patch.object(app, "utc_now_iso", return_value=ingested_at):
            result = app.handler(event, None)
        return result, json.loads(result["body"])

    def latest_item(self):
        item = fake_ddb.items[(f"STATION#{self.station_id}", "LATEST")]
        return {key: deserializer.deserialize(value) for key, value in item.items()}

    def observation_items(self):
        result = []
        for (pk, sk), item in fake_ddb.items.items():
            if pk == f"STATION#{self.station_id}" and sk.startswith("OBS#"):
                result.append({key: deserializer.deserialize(value) for key, value in item.items()})
        return sorted(result, key=lambda item: item["sk"])

    def test_normal_increasing_source_timestamps_advance_latest(self):
        self.post_observation(
            msg_id=100,
            source_ts="2026-08-04T12:00:00Z",
            received_at="2026-08-04T12:00:05Z",
            ingested_at="2026-08-04T12:00:06.000000Z",
        )
        normal_item = self.observation_items()[0]
        self.assertEqual(
            f"DISPLAY#2026-08-04T12:00:00.000000Z#{normal_item['sk']}",
            normal_item["history_sort"],
        )
        self.assertNotIn("history_sort", self.latest_item())
        result, body = self.post_observation(
            msg_id=101,
            source_ts="2026-08-04T12:01:00Z",
            received_at="2026-08-04T12:01:05Z",
            ingested_at="2026-08-04T12:01:06.000000Z",
            temperature=21,
        )

        latest = self.latest_item()
        self.assertEqual(200, result["statusCode"])
        self.assertFalse(body["deduped"])
        self.assertEqual(101, latest["msg_id"])
        self.assertEqual("2026-08-04T12:01:00.000000Z", latest["source_ts_utc"])
        self.assertEqual("2026-08-04T12:01:00.000000Z", latest["display_ts_utc"])
        self.assertFalse(latest["source_time_suspect"])
        self.assertEqual(Decimal("5"), latest["source_clock_skew_seconds"])

        latest_put = next(
            call
            for call in reversed(fake_ddb.put_calls)
            if call["Item"]["sk"] == {"S": "LATEST"}
        )
        self.assertIn("received_at_utc < :incoming_received_at", latest_put["ConditionExpression"])
        self.assertIn("ingested_at_utc < :incoming_ingested_at", latest_put["ConditionExpression"])
        self.assertEqual(
            {
                ":incoming_received_at",
                ":incoming_ingested_at",
            },
            set(latest_put["ExpressionAttributeValues"]),
        )

    def test_sub_threshold_source_rollback_is_suspect_against_previous_latest(self):
        self.post_observation(
            msg_id=200,
            source_ts="2026-08-04T12:10:00Z",
            received_at="2026-08-04T12:10:10Z",
            ingested_at="2026-08-04T12:10:11.000000Z",
        )
        _, body = self.post_observation(
            msg_id=201,
            source_ts="2026-08-04T12:09:30Z",
            received_at="2026-08-04T12:10:20Z",
            ingested_at="2026-08-04T12:10:21.000000Z",
        )

        latest = self.latest_item()
        self.assertLess(abs(body["source_clock_skew_seconds"]), 600)
        self.assertTrue(body["source_time_suspect"])
        self.assertEqual(201, latest["msg_id"])
        self.assertTrue(latest["source_time_suspect"])
        self.assertEqual("2026-08-04T12:10:20.000000Z", latest["display_ts_utc"])
        replay = next(item for item in self.observation_items() if item["msg_id"] == 201)
        self.assertEqual(
            f"DISPLAY#2026-08-04T12:10:20.000000Z#{replay['sk']}",
            replay["history_sort"],
        )

    def test_incident_sized_rollback_advances_latest_and_displays_receive_time(self):
        self.post_observation(
            msg_id=26629,
            source_ts="2026-08-04T23:07:01Z",
            received_at="2026-08-04T23:07:45Z",
            ingested_at="2026-08-04T23:07:46.000000Z",
        )
        _, body = self.post_observation(
            msg_id=26630,
            source_ts="2026-08-04T11:48:00Z",
            received_at="2026-08-04T23:08:45Z",
            ingested_at="2026-08-04T23:08:46.000000Z",
            temperature=22,
        )

        latest = self.latest_item()
        self.assertFalse(body["deduped"])
        self.assertTrue(body["source_time_suspect"])
        self.assertEqual(40845, body["source_clock_skew_seconds"])
        self.assertEqual(26630, latest["msg_id"])
        self.assertEqual("2026-08-04T11:48:00.000000Z", latest["source_ts_utc"])
        self.assertEqual("2026-08-04T23:08:45.000000Z", latest["received_at_utc"])
        self.assertEqual(latest["received_at_utc"], latest["display_ts_utc"])

    def test_future_source_clock_uses_absolute_skew_threshold(self):
        _, body = self.post_observation(
            msg_id=26631,
            source_ts="2026-08-04T12:20:00Z",
            received_at="2026-08-04T12:00:00Z",
            ingested_at="2026-08-04T12:00:01.000000Z",
        )

        latest = self.latest_item()
        self.assertEqual(-1200, body["source_clock_skew_seconds"])
        self.assertTrue(body["source_time_suspect"])
        self.assertEqual(latest["received_at_utc"], latest["display_ts_utc"])

    def test_older_received_history_does_not_replace_latest(self):
        self.post_observation(
            msg_id=300,
            source_ts="2026-08-04T12:10:00Z",
            received_at="2026-08-04T12:10:10Z",
            ingested_at="2026-08-04T12:10:11.000000Z",
        )
        self.post_observation(
            msg_id=301,
            source_ts="2026-08-04T12:10:30Z",
            received_at="2026-08-04T12:10:05Z",
            ingested_at="2026-08-04T12:10:12.000000Z",
        )

        self.assertEqual(2, len(self.observation_items()))
        self.assertEqual(300, self.latest_item()["msg_id"])

    def test_same_source_timestamp_with_distinct_message_ids_creates_two_rows(self):
        common_source_ts = "2026-08-04T12:00:00Z"
        self.post_observation(
            msg_id=400,
            source_ts=common_source_ts,
            received_at="2026-08-04T12:00:05Z",
            ingested_at="2026-08-04T12:00:06.000000Z",
        )
        self.post_observation(
            msg_id=401,
            source_ts=common_source_ts,
            received_at="2026-08-04T12:00:06Z",
            ingested_at="2026-08-04T12:00:07.000000Z",
        )

        rows = self.observation_items()
        keys = {row["sk"] for row in rows}
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len(keys))
        self.assertTrue(all(key.startswith("OBS#2026-08-04T12:00:00.000000Z#WEATHER#") for key in keys))
        self.assertEqual({400, 401}, {row["msg_id"] for row in rows})

    def test_receiver_suffix_survives_source_and_message_id_reuse(self):
        common = {
            "msg_id": 0,
            "source_ts": "2026-08-04T12:00:00Z",
        }
        self.post_observation(
            **common,
            received_at="2026-08-04T12:00:05Z",
            ingested_at="2026-08-04T12:00:06.000000Z",
        )
        self.post_observation(
            **common,
            received_at="2026-08-04T23:00:05Z",
            ingested_at="2026-08-04T23:00:06.000000Z",
        )

        rows = self.observation_items()
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len({row["sk"] for row in rows}))

    def test_exact_replay_is_deduped(self):
        request = {
            "msg_id": 500,
            "source_ts": "2026-08-04T12:00:00Z",
            "received_at": "2026-08-04T12:00:05Z",
        }
        self.post_observation(
            **request,
            ingested_at="2026-08-04T12:00:06.000000Z",
        )
        result, body = self.post_observation(
            **request,
            ingested_at="2026-08-04T12:05:00.000000Z",
        )

        self.assertEqual(200, result["statusCode"])
        self.assertTrue(body["deduped"])
        self.assertEqual(1, len(self.observation_items()))
        self.assertEqual(500, self.latest_item()["msg_id"])
        self.assertEqual(
            "2026-08-04T12:00:06.000000Z",
            self.latest_item()["ingested_at_utc"],
        )

    def test_exact_replay_repairs_a_missing_latest_write(self):
        request = {
            "msg_id": 501,
            "source_ts": "2026-08-04T12:00:00Z",
            "received_at": "2026-08-04T12:00:05Z",
        }
        self.post_observation(
            **request,
            ingested_at="2026-08-04T12:00:06.000000Z",
        )
        del fake_ddb.items[(f"STATION#{self.station_id}", "LATEST")]

        _, body = self.post_observation(
            **request,
            ingested_at="2026-08-04T12:05:00.000000Z",
            temperature=99,
        )

        self.assertTrue(body["deduped"])
        self.assertEqual(1, len(self.observation_items()))
        self.assertEqual(501, self.latest_item()["msg_id"])
        self.assertEqual(20, self.latest_item()["weather"]["air_temp_c"])
        self.assertEqual(
            "2026-08-04T12:00:06.000000Z",
            self.latest_item()["ingested_at_utc"],
        )

    def test_deduped_retry_repairs_equal_receive_time_ingestion_tie(self):
        common_received_at = "2026-08-04T12:00:05Z"
        self.post_observation(
            msg_id=600,
            source_ts="2026-08-04T12:00:00Z",
            received_at=common_received_at,
            ingested_at="2026-08-04T12:00:06.000000Z",
        )
        older_latest = copy.deepcopy(
            fake_ddb.items[(f"STATION#{self.station_id}", "LATEST")]
        )
        request = {
            "msg_id": 601,
            "source_ts": "2026-08-04T12:00:01Z",
            "received_at": common_received_at,
        }
        self.post_observation(
            **request,
            ingested_at="2026-08-04T12:00:07.000000Z",
            temperature=21,
        )
        fake_ddb.items[(f"STATION#{self.station_id}", "LATEST")] = older_latest

        _, body = self.post_observation(
            **request,
            ingested_at="2026-08-04T12:05:00.000000Z",
            temperature=99,
        )

        self.assertTrue(body["deduped"])
        self.assertEqual(601, self.latest_item()["msg_id"])
        self.assertEqual(21, self.latest_item()["weather"]["air_temp_c"])
        self.assertEqual(
            "2026-08-04T12:00:07.000000Z",
            self.latest_item()["ingested_at_utc"],
        )

    def test_legacy_unsuffixed_replay_dedupes_without_refreshing_ingestion(self):
        pk = f"STATION#{self.station_id}"
        source_ts = "2026-08-04T12:00:00.000000Z"
        received_at = "2026-08-04T12:00:05.000000Z"
        original_ingested_at = "2026-08-04T12:00:06.000000Z"
        legacy_sk = f"OBS#{source_ts}#WEATHER"
        legacy_observation = {
            "pk": pk,
            "sk": legacy_sk,
            "record_type": "observation",
            "observation_type": "WEATHER",
            "source_node_id": self.station_id,
            "source_name": "garden",
            "msg_id": 700,
            "source_ts_utc": source_ts,
            "source_ts_raw": "2026-08-04T12:00:00Z",
            "source_ts_sort_utc": source_ts,
            "received_at_utc": received_at,
            "weather": {"air_temp_c": 20},
            "raw_payload": {"payload": {}},
            "ingested_at_utc": original_ingested_at,
        }
        legacy_latest = {
            "pk": pk,
            "sk": "LATEST",
            "record_type": "latest",
            "observation_type": "WEATHER",
            "source_node_id": self.station_id,
            "source_name": "garden",
            "msg_id": 700,
            "source_ts_utc": source_ts,
            "source_ts_raw": "2026-08-04T12:00:00Z",
            "source_ts_sort_utc": source_ts,
            "received_at_utc": received_at,
            "weather": {"air_temp_c": 20},
            "latest_observation_sk": legacy_sk,
            "updated_at_utc": original_ingested_at,
        }
        fake_ddb.items[(pk, legacy_sk)] = app.serialize_item(legacy_observation)
        fake_ddb.items[(pk, "LATEST")] = app.serialize_item(legacy_latest)

        _, body = self.post_observation(
            msg_id=700,
            source_ts="2026-08-04T12:00:00Z",
            received_at="2026-08-04T12:00:05Z",
            ingested_at="2026-08-04T13:00:00.000000Z",
            temperature=99,
        )

        self.assertTrue(body["deduped"])
        self.assertEqual(legacy_sk, body["observation_sk"])
        self.assertEqual(1, len(self.observation_items()))
        self.assertEqual(20, self.latest_item()["weather"]["air_temp_c"])
        self.assertEqual(original_ingested_at, self.latest_item()["ingested_at_utc"])


if __name__ == "__main__":
    unittest.main()
