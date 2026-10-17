import copy
from datetime import datetime, timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest

import aggregate_history as history


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_module("rollup_worker_test", Path(__file__).with_name("app.py"))
backfill_path = Path(__file__).resolve().parents[2] / "scripts" / "aws" / "backfill_history_aggregates.py"
if not backfill_path.exists():
    backfill_path = Path(__file__).with_name("backfill_history_aggregates.py")
backfill = load_module("rollup_backfill_test", backfill_path)


def observation(stamp, key="a", weather=None, **extra):
    item = {"pk": "STATION#garden", "sk": "OBS#" + key, "display_ts_utc": history.iso(stamp),
            "weather": weather or {"air_temp_c": 10}}
    item.update(extra)
    item["history_sort"] = "DISPLAY#" + history.effective_time(item) + "#" + item["sk"]
    return item


class FakeDynamo:
    def __init__(self, observations=(), page_size=500):
        self.tables = {"raw": list(observations), "rollups": []}
        self.calls, self.page_size = [], page_size

    def get_item(self, **kwargs):
        key = history.decode_item(kwargs["Key"])
        match = next((row for row in self.tables[kwargs["TableName"]] if all(row[k] == v for k, v in key.items())), None)
        return {"Item": history.encode_item(match)} if match else {}

    def put_item(self, **kwargs):
        item = history.decode_item(kwargs["Item"])
        rows = self.tables[kwargs["TableName"]]
        rows[:] = [row for row in rows if (row["pk"], row["sk"]) != (item["pk"], item["sk"])]
        rows.append(item)

    def query(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        values = history.decode_item(kwargs["ExpressionAttributeValues"])
        sort_key = kwargs["ExpressionAttributeNames"]["#sort"]
        matches = sorted((row for row in self.tables[kwargs["TableName"]]
                          if row["pk"] == values[":pk"] and values[":lo"] <= row.get(sort_key, "") <= values[":hi"]), key=lambda row: row[sort_key])
        if kwargs.get("ExclusiveStartKey"):
            previous = history.decode_item(kwargs["ExclusiveStartKey"])
            matches = [row for row in matches if row[sort_key] > previous[sort_key]]
        limit = min(kwargs["Limit"], self.page_size)
        page = matches[:limit]
        result = {"Items": [history.encode_item(row) for row in page], "ScannedCount": len(page)}
        if len(matches) > limit:
            result["LastEvaluatedKey"] = history.encode_item({"pk": page[-1]["pk"], "sk": page[-1]["sk"], sort_key: page[-1][sort_key]})
        return result

    def backfill(self):
        rows, _ = backfill.build_rows(self.tables["raw"])
        self.tables["rollups"] = rows


class StatisticsTests(unittest.TestCase):
    def test_legacy_epoch_seconds_milliseconds_microseconds_and_decimals(self):
        # The known epoch is asserted independently of our unit-detection code.
        expected = datetime.fromtimestamp(1773794743, history.UTC).replace(microsecond=123456).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for value in ("1773794743.123456", Decimal("1773794743.123456"),
                      "1773794743123.456", Decimal("1773794743123456")):
            with self.subTest(value=value):
                self.assertEqual(expected, history.iso(value))
        self.assertEqual("2001-09-09T01:46:40.000000Z", history.iso("1000000000"))

    def test_legacy_indexed_time_is_authoritative_for_bucket_and_detail(self):
        canonical = history.iso("1773794743")
        row = {"source_ts_utc": "1773794743", "received_at_utc": "1773798343",
               "display_ts_utc": "1773794743", "history_sort": "DISPLAY#" + canonical + "#OBS#legacy"}
        self.assertEqual(canonical, history.effective_time(row))
        self.assertEqual(canonical, history.summarize_observations([row])["first"])

    def test_unindexed_legacy_numeric_clock_uses_receipt_when_suspect(self):
        row = {"source_ts_utc": Decimal("1773794743"), "received_at_utc": "1773798343000"}
        self.assertEqual(history.iso("1773798343"), history.effective_time(row))

    def test_invalid_epoch_numbers_are_rejected(self):
        for value in (True, -1, Decimal("NaN"), "9999999999999999999999999999999999999999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                history.parse_time(value)

    def test_weighted_average_min_max_and_last_survive_multiple_levels(self):
        a = history.summarize_observations([observation("2026-01-01T01:00:00Z", weather={"air_temp_c": 0})])
        b = history.summarize_observations([observation(f"2026-01-02T0{hour}:00:00Z", str(hour), {"air_temp_c": 30}) for hour in range(1, 4)])
        merged = history.merge_aggregates([a, b])
        self.assertEqual(Decimal("22.5"), history.weather_values(merged)["air_temp_c"])
        metric = history.public_summary(merged)["metrics"]["air_temp_c"]
        self.assertEqual((0, 30, 4, 30), (metric["min"], metric["max"], metric["count"], metric["last"]))

    def test_rain_never_sums_cumulative_daily_readings(self):
        rows = [observation("2026-01-01T01:00:00Z", "a", {"rain_interval_mm": 1, "local_day_rain_mm": 100}),
                observation("2026-01-01T02:00:00Z", "b", {"rain_interval_mm": 2, "local_day_rain_mm": 102}),
                observation("2026-01-01T03:00:00Z", "c", {"local_day_rain_mm": 200})]
        aggregate = history.summarize_observations(rows)
        self.assertEqual({"totalMm": 3, "reportedIntervalCount": 2, "eventCount": 2}, history.public_summary(aggregate)["rain"])
        self.assertEqual(3, history.weather_values(aggregate)["rain_interval_mm"])
        self.assertEqual(200, history.weather_values(aggregate)["local_day_rain_mm"])

    def test_lightning_distance_is_weighted_by_strikes(self):
        rows = [observation("2026-01-01T01:00:00Z", "a", {"lightning_strike_count": 1, "lightning_avg_distance_km": 10}),
                observation("2026-01-01T02:00:00Z", "b", {"lightning_strike_count": 3, "lightning_avg_distance_km": 30})]
        result = history.public_summary(history.summarize_observations(rows))["lightning"]
        self.assertEqual(25, result["averageDistanceKm"])
        self.assertEqual(10, result["closestDistanceKm"])
        self.assertEqual(4, result["totalStrikes"])

    def test_wind_direction_wraps_and_gust_preserves_peak(self):
        rows = [observation("2026-01-01T01:00:00Z", "a", {"wind_dir_deg": 350, "wind_gust_ms": 5}),
                observation("2026-01-01T02:00:00Z", "b", {"wind_dir_deg": 10, "wind_gust_ms": 20})]
        weather = history.weather_values(history.summarize_observations(rows))
        self.assertAlmostEqual(0, float(weather["wind_dir_deg"]), places=8)
        self.assertEqual(20, weather["wind_gust_ms"])

    def test_duplicate_timestamp_observations_are_distinct_and_last_is_deterministic(self):
        rows = [observation("2026-01-01T01:00:00Z", "b", {"air_temp_c": 20}),
                observation("2026-01-01T01:00:00Z", "a", {"air_temp_c": 10})]
        a, b = history.summarize_observations(rows), history.summarize_observations(reversed(rows))
        self.assertEqual(a, b)
        self.assertEqual(2, a["count"])
        self.assertEqual(20, a["metrics"]["air_temp_c"]["last"])

    def test_invalid_values_do_not_poison_statistics(self):
        rows = [observation("2026-01-01T01:00:00Z", weather={"air_temp_c": "NaN", "uv_index": True, "battery_voltage_v": "3.4"})]
        metrics = history.summarize_observations(rows)["metrics"]
        self.assertEqual({"battery_voltage_v"}, set(metrics))

    def test_suspect_source_uses_saved_receipt_not_upload(self):
        row = observation("2026-01-01T01:00:00Z", source_ts_utc="2026-01-01T01:00:00Z", received_at_utc="2026-01-01T12:00:00Z", ingested_at_utc="2026-01-03T10:00:00Z")
        self.assertEqual("2026-01-01T12:00:00.000000Z", history.effective_time(row))


class QueryTests(unittest.TestCase):
    def chart(self, db, start, end, points=800):
        return history.handle_chart_query(db, "raw", "rollups", "garden", start, end, points)

    def test_short_query_returns_original_detail_and_duplicate_timestamps(self):
        db = FakeDynamo([observation("2026-01-01T01:00:00Z", "a"), observation("2026-01-01T01:00:00Z", "b")], page_size=1)
        db.backfill()
        result = self.chart(db, "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
        self.assertEqual(("detail", "raw", 2), (result["mode"], result["resolution"], result["count"]))

    def test_six_month_window_uses_daily_summaries_and_only_edge_raw_queries(self):
        start = history.parse_time("2026-01-01T00:00:00Z")
        db = FakeDynamo([observation(start + timedelta(hours=hour), str(hour), {"air_temp_c": hour % 20}) for hour in range(180 * 24)])
        db.backfill()
        result = self.chart(db, "2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z")
        self.assertEqual(("day", 180, 4320), (result["resolution"], result["count"], result["totalMatched"]))
        raw_calls = [call for call in db.calls if call["TableName"] == "raw"]
        self.assertLessEqual(len(raw_calls), 2)
        self.assertLess(len(db.calls), 10)

    def test_partial_month_day_hour_edges_exclude_outside_observations(self):
        stamps = ["2026-01-01T01:14:59Z", "2026-01-01T01:15:00Z", "2026-01-15T12:00:00Z",
                  "2026-02-01T00:00:00Z", "2026-03-01T02:45:00Z", "2026-03-01T02:45:01Z"]
        db = FakeDynamo([observation(stamp, str(index), {"air_temp_c": index, "rain_interval_mm": 1}) for index, stamp in enumerate(stamps)])
        db.backfill()
        result = self.chart(db, "2026-01-01T01:15:00Z", "2026-03-01T02:45:00Z", 3)
        self.assertEqual("month", result["resolution"])
        self.assertEqual(4, result["totalMatched"])
        self.assertEqual(4, result["summary"]["rain"]["totalMm"])
        self.assertEqual(Decimal("2.5"), result["summary"]["metrics"]["air_temp_c"]["sum"] / 4)

    def test_ten_years_uses_months(self):
        db = FakeDynamo([observation(f"{year}-06-15T12:00:00Z", str(year)) for year in range(2016, 2026)])
        db.backfill()
        result = self.chart(db, "2016-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        self.assertEqual("month", result["resolution"])
        self.assertEqual(10, result["totalMatched"])

    def test_huge_requested_range_clips_to_retained_history(self):
        db = FakeDynamo([observation("2026-01-01T01:00:00Z")])
        db.backfill()
        result = self.chart(db, "0001-01-01T00:00:00Z", "9999-01-01T00:00:00Z")
        self.assertEqual(1, result["totalMatched"])
        self.assertEqual(1, len(db.calls))

    def test_outside_coverage_returns_empty_without_database_query(self):
        db = FakeDynamo([observation("2026-01-01T01:00:00Z")])
        db.backfill()
        self.assertEqual(0, self.chart(db, "2000-01-01T00:00:00Z", "2001-01-01T00:00:00Z")["count"])
        self.assertEqual([], db.calls)

    def test_not_ready_never_falls_back_to_unbounded_raw_scan(self):
        db = FakeDynamo()
        with self.assertRaisesRegex(ValueError, "history_aggregates_not_ready"):
            self.chart(db, "2000-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
        self.assertEqual([], db.calls)

    def test_row_budget_terminates_paginated_reads(self):
        db = FakeDynamo([observation(f"2026-01-01T01:0{minute}:00Z", str(minute)) for minute in range(5)], page_size=1)
        with self.assertRaisesRegex(ValueError, "history_query_budget_exceeded"):
            history.fetch_raw(db, "raw", "garden", history.parse_time("2026-01-01T01:00:00Z"), history.parse_time("2026-01-01T02:00:00Z"), history.QueryBudget(rows=2))
        self.assertEqual(2, len(db.calls))

    def test_coarsens_when_raw_point_limit_is_exceeded(self):
        db = FakeDynamo([observation(f"2026-01-01T01:{minute:02d}:00Z", str(minute)) for minute in range(10)])
        db.backfill()
        result = self.chart(db, "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z", 3)
        self.assertEqual(("aggregate", 1, 10), (result["mode"], result["count"], result["totalMatched"]))

    def test_years_group_to_respect_point_budget(self):
        db = FakeDynamo([observation(f"{year}-06-15T12:00:00Z", str(year)) for year in range(1900, 2026)])
        db.backfill()
        result = self.chart(db, "1800-01-01T00:00:00Z", "2030-01-01T00:00:00Z", 10)
        self.assertLessEqual(result["count"], 10)
        self.assertEqual(126, result["totalMatched"])


class WorkerTests(unittest.TestCase):
    def test_retries_and_late_arrivals_rebuild_all_parents_without_double_counting(self):
        first = observation("2026-01-01T01:05:00Z", "first", {"air_temp_c": 10, "rain_interval_mm": 1})
        db = FakeDynamo([first])
        db.backfill()
        late = observation("2026-01-01T01:10:00Z", "late", {"air_temp_c": 30, "rain_interval_mm": 2})
        db.tables["raw"].append(late)
        hour = history.floor_bucket(first["display_ts_utc"], "hour")
        pending = {"garden": {hour: {late["sk"]}}}
        worker.rebuild_hours(db, "raw", "rollups", pending)
        worker.rebuild_hours(db, "raw", "rollups", pending)
        for row in db.tables["rollups"]:
            if row["sk"] != "META":
                self.assertEqual(2, row["aggregate"]["count"])
                self.assertEqual(3, row["aggregate"]["rain"]["totalMm"])
                self.assertEqual(20, history.weather_values(row["aggregate"])["air_temp_c"])

    def test_gsi_lag_fails_before_publishing_incomplete_hour(self):
        first = observation("2026-01-01T01:05:00Z", "first")
        db = FakeDynamo([first])
        hour = history.floor_bucket(first["display_ts_utc"], "hour")
        with self.assertRaisesRegex(RuntimeError, "history_display_index_not_caught_up"):
            worker.rebuild_hours(db, "raw", "rollups", {"garden": {hour: {"OBS#not-visible-yet"}}})
        self.assertEqual([], db.tables["rollups"])

    def test_backfill_preserves_observation_count_across_levels_and_station_metadata(self):
        rows = [observation("2025-12-31T23:59:59Z", "a"), observation("2026-01-01T00:00:00Z", "b")]
        original = copy.deepcopy(rows)
        built, count = backfill.build_rows(rows)
        self.assertEqual(2, count)
        self.assertEqual(original, rows)
        for level in history.LEVELS:
            self.assertEqual(2, sum(row["aggregate"]["count"] for row in built if row.get("resolution") == level))
        meta = next(row for row in built if row["sk"] == "META")
        self.assertEqual("2025-12-31T23:59:59.000000Z", meta["availableFrom"])
        self.assertEqual("2026-01-01T00:00:00.000000Z", meta["availableTo"])


if __name__ == "__main__":
    unittest.main()
