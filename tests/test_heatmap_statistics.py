"""Synthetic statistics contracts; no network, account or business data."""
from datetime import datetime
import unittest

from time_logger.heatmap_statistics import build_statistics


def snapshot(values, start="2026-10-01", end="2026-10-08", as_of="2026-10-07T12:00:00+08:00"):
    return {"range_start": start, "range_end": end,
            "as_of_ms": int(datetime.fromisoformat(as_of).timestamp() * 1000),
            "summary": {"record_count": 5, "unknown_count": 2, "excluded_ai_count": 1,
                        "categories": {"工作": {"record_count": 2, "unknown_count": 1}}},
            "days": [{"date": day, "recorded_seconds": seconds, "record_count": 1,
                      "unknown_count": 0 if seconds else 1,
                      "categories": {"工作": {"recorded_seconds": seconds / 2 if seconds else None,
                                               "record_count": 1, "unknown_count": 0 if seconds else 1}}}
                     for day, seconds in values]}


class HeatmapStatisticsTests(unittest.TestCase):
    def test_average_uses_only_days_with_explicit_intervals(self):
        value = build_statistics(snapshot([("2026-10-01", 3600), ("2026-10-03", 7200)]))
        self.assertEqual(value["total_seconds"], 10800)
        self.assertEqual(value["recorded_days"], 2)
        self.assertEqual(value["average_seconds"], 5400)
        self.assertEqual(value["unknown_days"], 5)
        self.assertEqual(value["record_count"], 5)  # The unique range summary, not daily counts.
        self.assertEqual(value["peak_dates"], ["2026-10-03"])

    def test_category_uses_its_own_union_and_unique_summary(self):
        source = snapshot([("2026-10-01", 3600), ("2026-10-02", 7200)])
        value = build_statistics(source, "工作")
        self.assertEqual(value["total_seconds"], 5400)
        self.assertEqual(value["record_count"], 2)
        self.assertEqual(value["unknown_count"], 1)
        self.assertEqual(value["excluded_ai_count"], 0)
        self.assertIsNone(build_statistics(source, "学习")["total_seconds"])

    def test_unknown_cumulative_days_hold_known_sum_without_becoming_zero(self):
        value = build_statistics(snapshot([("2026-10-02", 3600), ("2026-10-04", 1800)]))
        daily = value["daily"]
        self.assertIsNone(daily[0]["seconds"])
        self.assertIsNone(daily[0]["cumulative_seconds"])
        self.assertIsNone(daily[2]["seconds"])
        self.assertEqual(daily[2]["cumulative_seconds"], 3600)
        self.assertFalse(daily[2]["has_interval"])
        self.assertEqual(daily[-1]["cumulative_seconds"], 5400)

    def test_estimate_components_follow_daily_weekly_cumulative_and_category_union(self):
        source = snapshot([("2026-10-01", 7200), ("2026-10-03", 3600)])
        source["summary"]["estimated_count"] = 2
        source["summary"]["categories"]["工作"]["estimated_count"] = 1
        for day, estimated in zip(source["days"], (1800, 3600)):
            day["estimated_seconds"], day["estimated_count"] = estimated, 1
            day["categories"]["工作"]["estimated_seconds"] = estimated / 2
            day["categories"]["工作"]["estimated_count"] = 1
        value = build_statistics(source)
        self.assertEqual(value["total_seconds"], 10800)
        self.assertEqual(value["estimated_seconds"], 5400)
        self.assertEqual(value["estimated_count"], 2)
        self.assertEqual(value["estimated_days"], 2)
        self.assertTrue(value["peak_estimated"])
        self.assertEqual(value["weekly"][0]["estimated_seconds"], 5400)
        self.assertEqual(value["daily"][1]["cumulative_estimated_seconds"], 1800)
        self.assertEqual(value["daily"][-1]["cumulative_estimated_seconds"], 5400)
        category = build_statistics(source, "工作")
        self.assertEqual(category["total_seconds"], 5400)
        self.assertEqual(category["estimated_seconds"], 2700)
        self.assertEqual(category["estimated_count"], 1)

    def test_future_estimates_and_invalid_components_do_not_inflate_totals(self):
        source = snapshot([("2026-10-01", 3600), ("2026-10-02", 7200)],
                          as_of="2026-10-01T12:00:00+08:00")
        source["days"][0]["estimated_seconds"] = 7200  # Cannot exceed its already deduplicated total.
        source["days"][1]["estimated_seconds"] = 7200
        source["days"][1]["estimated_count"] = 1
        value = build_statistics(source)
        self.assertEqual(value["estimated_seconds"], 3600)
        self.assertEqual(value["daily"][1]["estimated_seconds"], 0)
        self.assertEqual(value["daily"][1]["estimated_count"], 0)
        self.assertIsNone(value["daily"][1]["cumulative_estimated_seconds"])
        for invalid in (None, -1, True, "3600", float("nan"), float("inf")):
            source["days"][0]["estimated_seconds"] = invalid
            with self.subTest(value=invalid):
                self.assertEqual(build_statistics(source)["estimated_seconds"], 0)

    def test_streak_can_end_yesterday_but_does_not_skip_an_older_gap(self):
        source = snapshot([("2026-10-01", 60), ("2026-10-02", 60),
                           ("2026-10-04", 60), ("2026-10-05", 60), ("2026-10-06", 60)])
        value = build_statistics(source)
        self.assertEqual(value["current_streak"], 3)
        self.assertEqual(value["longest_streak"], 3)
        self.assertEqual(value["streak_end_date"], "2026-10-06")
        source["as_of_ms"] = int(datetime.fromisoformat("2026-10-09T12:00:00+08:00").timestamp() * 1000)
        source["range_end"] = "2026-10-10"
        self.assertEqual(build_statistics(source)["current_streak"], 0)

    def test_historical_range_streak_uses_selected_end(self):
        value = build_statistics(snapshot([("2026-10-06", 60), ("2026-10-07", 60)],
                                          as_of="2026-11-01T12:00:00+08:00"))
        self.assertEqual(value["current_streak"], 2)
        self.assertEqual(value["streak_end_date"], "2026-10-07")

    def test_week_is_monday_based_and_clipped_at_range_edges(self):
        values = [(f"2026-10-{day:02d}", 3600) for day in range(1, 15)]
        value = build_statistics(snapshot(values, end="2026-10-15", as_of="2026-10-14T12:00:00+08:00"))
        first, full, last = value["weekly"]
        self.assertEqual((first["start"], first["end"], first["seconds"]), ("2026-10-01", "2026-10-05", 14400))
        self.assertTrue(first["partial"])
        self.assertEqual((full["start"], full["end"], full["seconds"]), ("2026-10-05", "2026-10-12", 25200))
        self.assertFalse(full["partial"])
        self.assertTrue(last["partial"])

    def test_future_values_do_not_inflate_summary_or_cumulative_projection(self):
        value = build_statistics(snapshot([("2026-10-01", 3600), ("2026-10-02", 7200)],
                                          as_of="2026-10-01T12:00:00+08:00"))
        self.assertEqual(value["total_seconds"], 3600)
        self.assertEqual(value["elapsed_days"], 1)
        self.assertTrue(value["daily"][1]["future"])
        self.assertIsNone(value["daily"][1]["seconds"])
        self.assertIsNone(value["daily"][1]["cumulative_seconds"])

    def test_tied_peak_dates_and_duplicate_days_do_not_double_count(self):
        source = snapshot([("2026-10-01", 3600), ("2026-10-02", 3600), ("2026-10-01", 3600)])
        value = build_statistics(source)
        self.assertEqual(value["total_seconds"], 7200)
        self.assertEqual(value["peak_dates"], ["2026-10-01", "2026-10-02"])

    def test_missing_or_invalid_input_stays_unknown(self):
        for source in (None, {}, {"days": [None, {"date": "bad"}]}):
            with self.subTest(source=source):
                value = build_statistics(source)
                self.assertIsNone(value["total_seconds"])
                self.assertIsNone(value["average_seconds"])
                self.assertEqual(value["current_streak"], 0)
        source = snapshot([("2026-10-01", 1)])
        for invalid in (None, 0, -1, True, "3600", float("nan"), float("inf"), 90000):
            source["days"][0]["recorded_seconds"] = invalid
            with self.subTest(value=invalid):
                self.assertIsNone(build_statistics(source)["total_seconds"])

    def test_legacy_month_payload_is_supported_without_wall_clock(self):
        value = build_statistics({"month": "2026-02", "days": [{"date": "2026-02-01", "recorded_seconds": 60}]})
        self.assertEqual(value["range_start"], "2026-02-01")
        self.assertEqual(value["range_end"], "2026-03-01")
        self.assertEqual(value["as_of_date"], "2026-02-28")
        self.assertEqual(len(value["daily"]), 28)


if __name__ == "__main__":
    unittest.main()
