import importlib.util
import json
import os
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch


class FakeTypeDeserializer:
    def deserialize(self, value):
        return value


def load_app_module():
    os.environ.setdefault("TABLE_NAME", "WeatherObservationsTest")

    boto3_module = types.ModuleType("boto3")
    boto3_module.client = lambda service: object()

    boto3_dynamodb_module = types.ModuleType("boto3.dynamodb")
    boto3_types_module = types.ModuleType("boto3.dynamodb.types")
    boto3_types_module.TypeDeserializer = FakeTypeDeserializer

    sys.modules.setdefault("boto3", boto3_module)
    sys.modules.setdefault("boto3.dynamodb", boto3_dynamodb_module)
    sys.modules.setdefault("boto3.dynamodb.types", boto3_types_module)

    module_path = Path(__file__).with_name("app.py")
    spec = importlib.util.spec_from_file_location("weather_read_app", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load_app_module()


class BuildHistorySummaryTests(unittest.TestCase):
    def test_sums_unrounded_rain_intervals_and_collects_event_rows(self):
        items = [
            {"sk": "one", "weather": {"rain_interval_mm": Decimal("0.0254")}},
            {"sk": "two", "weather": {"rain_interval_mm": Decimal("0")}},
            {"sk": "three", "weather": {"rain_interval_mm": Decimal("0.0762")}},
        ]

        summary = app.build_history_summary(items)

        self.assertEqual(Decimal("0.1016"), summary["rain"]["totalMm"])
        self.assertEqual(3, summary["rain"]["reportedIntervalCount"])
        self.assertEqual(2, summary["rain"]["eventCount"])
        self.assertEqual(["one", "three"], [
            item["sk"] for item in summary["rain"]["eventItems"]
        ])

    def test_matches_existing_lightning_stat_semantics(self):
        items = [
            {
                "weather": {
                    "lightning_strike_count": Decimal("2"),
                    "lightning_avg_distance_km": Decimal("10"),
                },
            },
            {
                "weather": {
                    "lightning_strike_count": Decimal("1"),
                    "lightning_avg_distance_km": Decimal("4"),
                },
            },
            {"weather": {"lightning_strike_count": Decimal("0")}},
        ]

        lightning = app.build_history_summary(items)["lightning"]

        self.assertEqual(2, lightning["activePoints"])
        self.assertEqual(Decimal("8"), lightning["averageDistanceKm"])
        self.assertEqual(Decimal("4"), lightning["closestDistanceKm"])
        self.assertEqual(3, lightning["reportedPointCount"])
        self.assertEqual(Decimal("3"), lightning["totalStrikes"])

    def test_returns_null_sections_when_metrics_are_absent(self):
        summary = app.build_history_summary([{"weather": {"air_temp_c": 20}}])

        self.assertIsNone(summary["rain"])
        self.assertIsNone(summary["lightning"])


class HistoryResponseCompatibilityTests(unittest.TestCase):
    def test_sampled_response_keeps_existing_fields_and_adds_summary(self):
        items = [
            {
                "sk": "OBS#2026-07-01T00:00:00Z#WEATHER",
                "weather": {
                    "rain_interval_mm": Decimal("0.0254"),
                    "lightning_strike_count": Decimal("0"),
                },
            },
            {
                "sk": "OBS#2026-07-01T00:01:00Z#WEATHER",
                "weather": {
                    "rain_interval_mm": Decimal("0.0762"),
                    "lightning_strike_count": Decimal("0"),
                },
            },
        ]
        event = {
            "queryStringParameters": {
                "stationId": "garden",
                "from": "2026-07-01T00:00:00Z",
                "to": "2026-07-31T23:59:59Z",
                "sample": "1",
            },
        }

        with patch.object(app, "fetch_all_items_for_sampling", return_value=items):
            result = app.handle_get_history(event)

        body = json.loads(result["body"])

        self.assertEqual(200, result["statusCode"])
        self.assertEqual("sampled", body["mode"])
        self.assertEqual("garden", body["stationId"])
        self.assertEqual(1, body["sampleRequested"])
        self.assertEqual(2, body["totalMatched"])
        self.assertEqual(1, body["count"])
        self.assertEqual(1, len(body["items"]))
        self.assertEqual(0.1016, body["summary"]["rain"]["totalMm"])


class LatestTimestampMetadataTests(unittest.TestCase):
    def test_latest_response_exposes_persisted_timestamp_metadata(self):
        latest_item = {
            "pk": "STATION#!5c32d4d9",
            "sk": "LATEST",
            "source_node_id": "!5c32d4d9",
            "msg_id": 0,
            "source_ts_utc": "2026-08-04T11:48:00.000000Z",
            "received_at_utc": "2026-08-04T23:08:00.000000Z",
            "ingested_at_utc": "2026-08-04T23:08:01.000000Z",
            "display_ts_utc": "2026-08-04T23:08:00.000000Z",
            "source_time_suspect": True,
            "source_clock_skew_seconds": Decimal("40800"),
            "weather": {"air_temp_c": Decimal("21.5")},
        }
        fake_ddb = MagicMock()
        fake_ddb.get_item.return_value = {"Item": latest_item}

        with patch.object(app, "ddb", fake_ddb):
            result = app.handle_get_latest(
                {"queryStringParameters": {"stationId": "!5c32d4d9"}}
            )

        body = json.loads(result["body"])
        item = body["item"]

        self.assertEqual(200, result["statusCode"])
        self.assertEqual("2026-08-04T11:48:00.000000Z", item["source_ts_utc"])
        self.assertEqual("2026-08-04T23:08:00.000000Z", item["received_at_utc"])
        self.assertEqual("2026-08-04T23:08:01.000000Z", item["ingested_at_utc"])
        self.assertEqual("2026-08-04T23:08:00.000000Z", item["display_ts_utc"])
        self.assertTrue(item["source_time_suspect"])
        self.assertEqual(40800, item["source_clock_skew_seconds"])
        self.assertEqual(0, item["msg_id"])
        self.assertEqual(21.5, item["weather"]["air_temp_c"])

    def test_latest_response_derives_metadata_for_legacy_latest_item(self):
        legacy_item = {
            "pk": "STATION#garden",
            "sk": "LATEST",
            "source_ts_utc": "2026-08-04T12:00:00.000000Z",
            "received_at_utc": "2026-08-04T12:00:44.000000Z",
            "updated_at_utc": "2026-08-04T12:00:45.000000Z",
            "msg_id": 26629,
            "weather": {"relative_humidity_pct": Decimal("61")},
        }
        fake_ddb = MagicMock()
        fake_ddb.get_item.return_value = {"Item": legacy_item}

        with patch.object(app, "ddb", fake_ddb):
            result = app.handle_get_latest(
                {"queryStringParameters": {"stationId": "garden"}}
            )

        item = json.loads(result["body"])["item"]

        self.assertFalse(item["source_time_suspect"])
        self.assertEqual(44, item["source_clock_skew_seconds"])
        self.assertEqual("2026-08-04T12:00:00.000000Z", item["display_ts_utc"])
        self.assertEqual("2026-08-04T12:00:45.000000Z", item["ingested_at_utc"])


class HistoryTimestampMetadataTests(unittest.TestCase):
    def test_history_bounds_normalize_exact_seconds_offsets_and_epoch(self):
        self.assertEqual(
            "2026-08-04T11:48:00.000000Z",
            app.normalize_history_bound("2026-08-04T11:48:00Z"),
        )
        self.assertEqual(
            "2026-08-04T11:48:00.000000Z",
            app.normalize_history_bound("2026-08-04T07:48:00-04:00"),
        )
        self.assertEqual(
            "1970-01-01T00:00:00.000000Z",
            app.normalize_history_bound("0"),
        )

        fake_ddb = MagicMock()
        fake_ddb.query.return_value = {"Items": []}
        event = {
            "queryStringParameters": {
                "stationId": "!5c32d4d9",
                "from": "2026-08-04T11:48:00Z",
                "to": "2026-08-04T11:48:00Z",
            }
        }

        with patch.object(app, "ddb", fake_ddb):
            result = app.handle_get_history(event)

        query_values = fake_ddb.query.call_args.kwargs["ExpressionAttributeValues"]
        self.assertEqual(200, result["statusCode"])
        self.assertEqual(
            "OBS#2026-08-04T11:48:00.000000Z",
            query_values[":from"]["S"],
        )
        self.assertEqual(
            "OBS#2026-08-04T11:48:00.000000Z~",
            query_values[":to"]["S"],
        )

    def test_sample_projection_requests_all_timestamp_metadata(self):
        fake_ddb = MagicMock()
        fake_ddb.query.return_value = {"Items": []}

        with patch.object(app, "ddb", fake_ddb):
            app.query_history_page(
                "STATION#garden",
                "OBS#2026-08-04T00:00:00.000000Z",
                "OBS#2026-08-04T23:59:59.999999Z~",
                projection=True,
            )

        projection = fake_ddb.query.call_args.kwargs["ProjectionExpression"]
        projected_fields = {field.strip() for field in projection.split(",")}

        self.assertTrue(
            {
                "source_ts_utc",
                "received_at_utc",
                "ingested_at_utc",
                "display_ts_utc",
                "source_time_suspect",
                "source_clock_skew_seconds",
                "msg_id",
                "weather",
            }.issubset(projected_fields)
        )

    def test_sample_fetch_preserves_suffixed_last_evaluated_key(self):
        last_evaluated_key = {
            "pk": {"S": "STATION#!5c32d4d9"},
            "sk": {
                "S": (
                    "OBS#2026-08-04T11:48:00.000000Z#WEATHER"
                    "#RECEIVED#2026-08-04T23:08:00.000000Z#MSG#0"
                )
            },
        }
        pages = [
            {
                "Items": [
                    {
                        "sk": last_evaluated_key["sk"]["S"],
                        "source_ts_utc": "2026-08-04T11:48:00.000000Z",
                        "received_at_utc": "2026-08-04T23:08:00.000000Z",
                        "ingested_at_utc": "2026-08-04T23:08:01.000000Z",
                        "display_ts_utc": "2026-08-04T23:08:00.000000Z",
                        "source_time_suspect": True,
                        "source_clock_skew_seconds": Decimal("40800"),
                        "msg_id": 0,
                        "weather": {"air_temp_c": Decimal("21.5")},
                    }
                ],
                "LastEvaluatedKey": last_evaluated_key,
            },
            {
                "Items": [
                    {
                        "sk": (
                            "OBS#2026-08-04T11:49:00.000000Z#WEATHER"
                            "#RECEIVED#2026-08-04T23:09:00.000000Z#MSG#1"
                        ),
                        "source_ts_utc": "2026-08-04T11:49:00.000000Z",
                        "received_at_utc": "2026-08-04T23:09:00.000000Z",
                        "ingested_at_utc": "2026-08-04T23:09:01.000000Z",
                        "display_ts_utc": "2026-08-04T23:09:00.000000Z",
                        "source_time_suspect": True,
                        "source_clock_skew_seconds": Decimal("40800"),
                        "msg_id": 1,
                        "weather": {"air_temp_c": Decimal("21.6")},
                    }
                ]
            },
        ]

        with patch.object(app, "query_history_page", side_effect=pages) as query:
            items = app.fetch_all_items_for_sampling(
                "STATION#!5c32d4d9",
                "OBS#2026-08-04T11:48:00.000000Z",
                "OBS#2026-08-04T11:49:00.000000Z~",
            )

        self.assertEqual(2, len(items))
        self.assertTrue(all(item["source_time_suspect"] for item in items))
        self.assertEqual(
            "2026-08-04T23:09:00.000000Z",
            items[1]["display_ts_utc"],
        )
        self.assertIsNone(query.call_args_list[0].kwargs["exclusive_start_key"])
        self.assertEqual(
            last_evaluated_key,
            query.call_args_list[1].kwargs["exclusive_start_key"],
        )

    def test_raw_source_time_range_keeps_distinct_replayed_timestamps(self):
        source_ts = "2026-08-04T11:48:00.000000Z"
        legacy_sk = f"OBS#{source_ts}#WEATHER"
        first_sk = (
            f"OBS#{source_ts}#WEATHER"
            "#RECEIVED#2026-08-04T11:48:45.000000Z#MSG#26629"
        )
        replay_sk = (
            f"OBS#{source_ts}#WEATHER"
            "#RECEIVED#2026-08-04T23:08:00.000000Z#MSG#0"
        )
        fake_ddb = MagicMock()
        fake_ddb.query.return_value = {
            "Items": [
                {
                    "sk": legacy_sk,
                    "source_ts_utc": source_ts,
                    "received_at_utc": "2026-08-04T11:48:44.000000Z",
                    "msg_id": 26628,
                    "weather": {"air_temp_c": Decimal("20.0")},
                },
                {
                    "sk": first_sk,
                    "source_ts_utc": source_ts,
                    "received_at_utc": "2026-08-04T11:48:45.000000Z",
                    "msg_id": 26629,
                    "weather": {"air_temp_c": Decimal("20.1")},
                },
                {
                    "sk": replay_sk,
                    "source_ts_utc": source_ts,
                    "received_at_utc": "2026-08-04T23:08:00.000000Z",
                    "msg_id": 0,
                    "weather": {"air_temp_c": Decimal("21.5")},
                },
            ]
        }
        event = {
            "queryStringParameters": {
                "stationId": "!5c32d4d9",
                "from": source_ts,
                "to": source_ts,
                "order": "asc",
            }
        }

        with patch.object(app, "ddb", fake_ddb):
            result = app.handle_get_history(event)

        body = json.loads(result["body"])
        query_values = fake_ddb.query.call_args.kwargs["ExpressionAttributeValues"]
        sk_from = query_values[":from"]["S"]
        sk_to = query_values[":to"]["S"]

        self.assertEqual(3, body["count"])
        self.assertEqual(
            [legacy_sk, first_sk, replay_sk],
            [item["sk"] for item in body["items"]],
        )
        self.assertEqual(f"OBS#{source_ts}", sk_from)
        self.assertEqual(f"OBS#{source_ts}~", sk_to)
        self.assertTrue(all(sk_from <= key <= sk_to for key in body_sk_values(body)))

    def test_raw_pagination_token_round_trips_full_suffixed_key(self):
        last_evaluated_key = {
            "pk": {"S": "STATION#!5c32d4d9"},
            "sk": {
                "S": (
                    "OBS#2026-08-04T11:48:00.000000Z#WEATHER"
                    "#RECEIVED#2026-08-04T23:08:00.000000Z#MSG#0"
                )
            },
        }
        fake_ddb = MagicMock()
        fake_ddb.query.side_effect = [
            {"Items": [], "LastEvaluatedKey": last_evaluated_key},
            {"Items": []},
        ]
        base_query = {
            "stationId": "!5c32d4d9",
            "from": "2026-08-04T00:00:00.000000Z",
            "to": "2026-08-04T23:59:59.999999Z",
        }

        with patch.object(app, "ddb", fake_ddb):
            first_result = app.handle_get_history(
                {"queryStringParameters": base_query}
            )
            token = json.loads(first_result["body"])["nextToken"]
            second_result = app.handle_get_history(
                {
                    "queryStringParameters": {
                        **base_query,
                        "nextToken": token,
                    }
                }
            )

        self.assertEqual(200, second_result["statusCode"])
        self.assertEqual(
            last_evaluated_key,
            fake_ddb.query.call_args_list[1].kwargs["ExclusiveStartKey"],
        )


def body_sk_values(body):
    return [item["sk"] for item in body["items"]]


class DisplayTimeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.index_patch = patch.object(app, "HISTORY_INDEX_NAME", "ObservationDisplayTimeIndex")
        self.index_patch.start()
        self.addCleanup(self.index_patch.stop)
        self.items = []
        for source, received, msg, rain in [
            ("02:17:57", "13:39:41", 1, "0.1"),
            ("02:17:57", "13:39:41", 2, "0.2"),
            ("09:29:53", "20:51:29", 3, "0.3"),
            # Its source time is in the window, but its display time is outside it.
            ("14:00:00", "23:00:00", 4, "100"),
            ("12:00:00", "12:00:05", 5, "100"),
        ]:
            item = {
                "pk": "STATION#garden",
                "sk": f"OBS#2026-10-16T{source}.000000Z#WEATHER#MSG#{msg}",
                "source_ts_utc": f"2026-10-16T{source}Z",
                "received_at_utc": f"2026-10-16T{received}Z",
                "weather": {"rain_interval_mm": Decimal(rain)},
            }
            item["history_sort"] = app.build_display_history_sort(item)
            self.items.append(item)
        self.query_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        self.assertEqual("ObservationDisplayTimeIndex", kwargs["IndexName"])
        self.assertEqual("history_sort", kwargs["ExpressionAttributeNames"]["#history_key"])
        values = kwargs["ExpressionAttributeValues"]
        rows = sorted(
            [item for item in self.items if item["pk"] == values[":pk"]["S"]
             and values[":from"]["S"] <= item["history_sort"] <= values[":to"]["S"]],
            key=lambda item: item["history_sort"],
            reverse=not kwargs["ScanIndexForward"],
        )
        cursor = kwargs.get("ExclusiveStartKey")
        if cursor:
            position = next(i for i, item in enumerate(rows) if item["sk"] == cursor["sk"]["S"])
            rows = rows[position + 1:]
        # Force DynamoDB pagination even in sampled mode.
        page_size = min(kwargs.get("Limit", 2), 2)
        page = rows[:page_size]
        result = {"Items": page}
        if len(rows) > page_size:
            result["LastEvaluatedKey"] = {key: {"S": page[-1][key]} for key in ("pk", "sk", "history_sort")}
        return result

    def request(self, **params):
        query = {"stationId": "garden", "from": "2026-10-16T13:39:00Z", "to": "2026-10-16T20:55:00Z", **params}
        with patch.object(app, "ddb", types.SimpleNamespace(query=self.query)):
            response = app.handle_get_history({"queryStringParameters": query})
        self.assertEqual(200, response["statusCode"])
        return json.loads(response["body"])

    def test_custom_sample_uses_display_time_and_summarizes_all_matching_pages(self):
        body = self.request(sample="2")
        self.assertEqual(3, body["totalMatched"])
        self.assertEqual(2, body["count"])
        self.assertAlmostEqual(0.6, body["summary"]["rain"]["totalMm"])
        self.assertEqual(3, body["summary"]["rain"]["eventCount"])
        self.assertEqual(2, len(self.query_calls))
        self.assertTrue(all(item["source_time_suspect"] for item in body["items"]))

    def test_one_hour_range_finds_rows_whose_source_time_is_outside_window(self):
        body = self.request(sample="800", **{"from": "2026-10-16T20:00:00Z", "to": "2026-10-16T21:00:00Z"})
        self.assertEqual(1, body["totalMatched"])
        self.assertEqual("2026-10-16T09:29:53Z", body["items"][0]["source_ts_utc"])

    def test_raw_pagination_in_both_orders_preserves_equal_display_timestamps(self):
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                found = []
                token = None
                while True:
                    body = self.request(limit="1", order=order, **({"nextToken": token} if token else {}))
                    found.extend(item["sk"] for item in body["items"])
                    token = body.get("nextToken")
                    if not token:
                        break
                    self.assertIn("history_sort", app.decode_next_token(token))
                expected = [item["sk"] for item in self.items[:3]]
                self.assertEqual(expected if order == "asc" else expected[::-1], found)

    def test_exact_bound_includes_all_ties_and_normalizes_offsets(self):
        body = self.request(sample="800", **{"from": "2026-10-16T09:39:41-04:00", "to": "2026-10-16T13:39:41Z"})
        self.assertEqual(2, body["totalMatched"])

    def test_healthy_clock_keeps_source_time_and_does_not_include_latest(self):
        self.assertTrue(self.items[-1]["history_sort"].startswith("DISPLAY#2026-10-16T12:00:00.000000Z#"))
        body = self.request(sample="800", **{"from": "2026-10-16T12:00:00Z", "to": "2026-10-16T12:00:00Z"})
        self.assertEqual(1, body["totalMatched"])
        self.assertFalse(body["items"][0]["source_time_suspect"])

    def test_old_source_index_token_is_rejected_cleanly(self):
        token = app.encode_next_token({"pk": {"S": "STATION#garden"}, "sk": {"S": "OBS#old"}})
        response = app.handle_get_history({"queryStringParameters": {
            "stationId": "garden", "from": "2026-10-16T00:00:00Z", "to": "2026-10-16T23:59:59Z", "nextToken": token,
        }})
        self.assertEqual(400, response["statusCode"])
        self.assertEqual("invalid_nextToken", json.loads(response["body"])["error"])


if __name__ == "__main__":
    unittest.main()
