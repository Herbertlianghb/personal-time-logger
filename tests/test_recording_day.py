"""Activity-day boundaries and unorganized reports use synthetic evidence only."""
import unittest
from datetime import datetime
from unittest.mock import patch

from tests import test_timeline as fixtures
from tests.test_timeline import DAY, LEFT, record
from tests import test_heatmap as heatmap_fixtures


class RecordingDayTests(unittest.TestCase):
    setUp = fixtures.TimelineTests.setUp
    tearDown = fixtures.TimelineTests.tearDown
    reader = fixtures.TimelineTests.reader
    evidence = fixtures.TimelineTests.evidence
    mapped = fixtures.TimelineTests.mapped
    def test_seven_am_window_includes_next_morning_and_clips_crossing(self):
        rows = [record("early", LEFT + 6 * 3600000, LEFT + 7 * 3600000),
                record("cross", LEFT + 6 * 3600000, LEFT + 8 * 3600000),
                record("next", LEFT + 26 * 3600000, LEFT + 27 * 3600000),
                record("after", LEFT + 31 * 3600000, LEFT + 32 * 3600000)]
        result = self.reader(rows).history(DAY, day_start_hour=7)
        self.assertEqual(result["day_start_ms"], LEFT + 7 * 3600000)
        self.assertEqual(result["day_end_ms"], LEFT + 31 * 3600000)
        self.assertEqual({i["record_id"] for i in result["items"]}, {"cross", "next"})
        self.assertEqual(result["recorded_totals"]["categories"]["工作"]["recorded_seconds"], 7200)

    def test_feishu_report_is_visible_before_organization_without_counting_time(self):
        oid = self.evidence("尚未整理的飞书报告")
        result = self.reader([]).history(DAY, day_start_hour=7)
        self.assertEqual([i.get("observation_id") for i in result["items"]], [oid])
        item = result["items"][0]
        self.assertEqual(item["source"], "feishu_raw")
        self.assertEqual(item["report_ms"], LEFT + 12 * 3600000)
        self.assertEqual(item["recorded_intervals"], [])
        self.assertIsNone(result["summary"]["human_seconds"])

    def test_organized_report_is_not_duplicated_and_recall_removes_raw_node(self):
        row = record("formal", LEFT + 12 * 3600000, LEFT + 13 * 3600000)
        block = self.mapped(row)
        reader = self.reader([row])
        self.assertEqual(len(reader.history(DAY, day_start_hour=7)["items"]), 1)
        oid = self.evidence("另一条尚未整理的报告")
        self.assertEqual(len(reader.history(DAY, day_start_hour=7)["items"]), 2)
        with self.store.transaction() as db:
            db.execute("UPDATE observations SET kind='recall' WHERE observation_id=?", (oid,))
        self.assertEqual(len(reader.history(DAY, day_start_hour=7)["items"]), 1)

    def test_cache_and_cursor_do_not_cross_day_boundaries(self):
        reader = self.reader([record("early", LEFT + 2 * 3600000, LEFT + 3 * 3600000),
                              record("late", LEFT + 12 * 3600000, LEFT + 13 * 3600000)])
        natural = reader.history(DAY, limit=1)
        shifted = reader.history(DAY, day_start_hour=7)
        self.assertEqual(shifted["summary"]["recorded_seconds"], 3600)
        with self.assertRaises(ValueError):
            reader.history(DAY, natural["next_cursor"], day_start_hour=7)

    def test_approximate_formal_windows_keep_heatmap_uncertainty(self):
        row = record("estimate", LEFT + 26 * 3600000, LEFT + 27 * 3600000,
                     **{"备注": "结束采用报告时间估算；实际切换分钟未知"})
        result = self.reader([row]).history(DAY, day_start_hour=7)
        metrics = result["recorded_totals"]["categories"]["工作"]
        self.assertEqual(metrics["recorded_seconds"], 3600)
        self.assertEqual(metrics["estimated_seconds"], 3600)
        self.assertIsNone(result["summary"]["human_seconds"])

    def test_invalid_day_start_is_rejected(self):
        for hour in (-1, 24, True, "7"):
            with self.assertRaises(ValueError):
                self.reader([]).history(DAY, day_start_hour=hour)

    def test_recording_date_changes_at_seven_including_month_and_year(self):
        from time_logger.day_window import recording_date
        for point, expected in (("2026-11-03T06:59:59+08:00", "2026-11-02"),
                                ("2026-11-03T07:00:00+08:00", "2026-11-03"),
                                ("2027-01-01T02:00:00+08:00", "2026-12-31")):
            self.assertEqual(recording_date(datetime.fromisoformat(point)).isoformat(), expected)


class HeatmapDayTests(unittest.TestCase):
    setUp = heatmap_fixtures.HeatmapTests.setUp
    reader = heatmap_fixtures.HeatmapTests.reader

    def test_heatmap_days_match_seven_am_history_through_month_edge(self):
        from time_logger.timeline import TimelineReader
        rows = [record("cross", fixtures.stamp("2026-10-01T06:30:00+08:00"), fixtures.stamp("2026-10-01T07:30:00+08:00")),
                record("night", fixtures.stamp("2026-10-02T02:00:00+08:00"), fixtures.stamp("2026-10-02T03:00:00+08:00")),
                record("next-month", fixtures.stamp("2026-11-01T05:00:00+08:00"), fixtures.stamp("2026-11-01T06:00:00+08:00")),
                record("outside", fixtures.stamp("2026-11-01T07:00:00+08:00"), fixtures.stamp("2026-11-01T08:00:00+08:00"))]
        self.now = fixtures.stamp("2026-11-02T12:00:00+08:00")
        result = self.reader(rows).month("2026-10")
        self.assertEqual(result["days"][0]["recorded_seconds"], 5400)
        self.assertEqual(result["day_start_hour"], 7)
        self.assertEqual(result["days"][-1]["recorded_seconds"], 3600)
        timeline = TimelineReader(self.store, fixtures.ReadTransport(rows), clock=lambda: self.now)
        for day in (result["days"][0], result["days"][-1]):
            detail = timeline.history(day["date"], day_start_hour=7)
            for category in detail["recorded_totals"]["categories"]:
                self.assertEqual(day["categories"][category], detail["recorded_totals"]["categories"][category])
        self.assertEqual(result["summary"]["recorded_seconds"], 9000)

    def test_early_january_belongs_to_last_activity_day_of_previous_year(self):
        from time_logger.heatmap_statistics import build_statistics
        self.now = fixtures.stamp("2027-01-01T06:30:00+08:00")
        rows = [record("year-edge", fixtures.stamp("2027-01-01T01:00:00+08:00"), fixtures.stamp("2027-01-01T02:00:00+08:00"))]
        result = self.reader(rows).month("2026-12", scope="year")
        self.assertEqual(result["days"][-1]["recorded_seconds"], 3600)
        stats = build_statistics(result)
        self.assertEqual(stats["as_of_date"], "2026-12-31")
        self.assertEqual(stats["weekly"][-1]["seconds"], 3600)
        self.assertEqual(stats["daily"][-1]["cumulative_seconds"], 3600)

    def test_natural_day_cache_is_not_used_as_activity_day_fallback(self):
        from time_logger.store import digest
        reader = self.reader([])
        self.store.set_setting("desktop_heatmap_cache:" + digest(self.transport.table_key)[:24] + ":2026-10",
            {"month": "2026-10", "as_of_ms": self.now, "local_state_hash": "old", "days": [], "summary": {}})
        self.transport.fail = True
        with self.assertRaises(OSError):
            reader.month("2026-10")
