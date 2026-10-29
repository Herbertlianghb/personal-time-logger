"""Synthetic monthly read contracts. No accounts, network or business data."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from time_logger.base_adapter import ALLOWLIST
from time_logger.heatmap import HeatmapReader, _merge_heatmap_items, _totals, month_bounds, period_bounds
from time_logger.store import Store, digest
from time_logger.timeline import _timestamp


def stamp(value):
    return _timestamp(value)


def row(rid, start=None, end=None, **fields):
    return {"record_id": rid, "fields": {"时间花销": "虚构活动", "开始时间": start, "结束时间": end,
                                         "一级分类": ["工作"], "备注": "", **fields}}


class ReadTransport:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.calls, self.page_size, self.fail, self.bad_pagination = 0, 200, False, False
        self.table_key = ("synthetic", "base", "table")

    def field_schema(self):
        self.calls += 1
        if self.fail:
            raise OSError("synthetic_unavailable")
        return {"fields": [{"name": name, "type": kind} for name, kind in ALLOWLIST.items()]}

    def get(self, rid):
        return next((copy.deepcopy(item) for item in self.rows if item["record_id"] == rid), None)

    def _call(self, command, *args):
        self.calls += 1
        self.assert_command = command
        opts = dict(zip(args[::2], args[1::2]))
        conditions = json.loads(opts["--filter-json"])["conditions"]

        def selected(item):
            for name, op, expression in conditions:
                value = item["fields"].get(name)
                point = stamp(expression[len("ExactDate("):-1] + "T00:00:00+08:00")
                if value is None or (op == "<" and value >= point) or (op == ">" and value <= point):
                    return False
            return True

        chosen = [item for item in self.rows if selected(item)]
        offset = int(opts["--offset"])
        page = chosen[offset:offset + self.page_size]
        names = list(ALLOWLIST)
        return {"fields": names, "record_id_list": [item["record_id"] for item in page],
                "data": [[item["fields"].get(name) for name in names] for item in page],
                "has_more": offset + len(page) < len(chosen),
                "next_offset": offset if self.bad_pagination else offset + len(page)}


class HeatmapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ptl-month-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3", "synthetic", ["self"])
        self.addCleanup(self.store.close)
        self.now = stamp("2026-11-01T00:00:00+08:00")

    def reader(self, rows):
        self.transport = ReadTransport(rows)
        return HeatmapReader(self.store, self.transport, clock=lambda: self.now)

    def test_month_validation(self):
        self.assertEqual(month_bounds("2026-12")[1].isoformat(), "2027-01-01")
        for value in (None, "2026-1", "2026-13", "1999-12", "9999-12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                month_bounds(value)

    def test_rolling_year_bounds_and_scope_validation(self):
        self.assertEqual(tuple(day.isoformat() for day in period_bounds("2026-10", "year")),
                         ("2025-11-01", "2026-11-01"))
        self.assertEqual(tuple(day.isoformat() for day in period_bounds("2026-01", "year")),
                         ("2025-02-01", "2026-02-01"))
        self.assertEqual(tuple(day.isoformat() for day in period_bounds("2000-01", "year")),
                         ("2000-01-01", "2000-02-01"))
        self.assertEqual(period_bounds("2026-10"), month_bounds("2026-10"))
        for scope in (None, "all", "week", [], True):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "invalid_heatmap_scope"):
                period_bounds("2026-10", scope)

    def test_year_uses_one_bounded_range_read_and_unique_activity_counts(self):
        reader = self.reader([
            row("left-edge", stamp("2025-10-31T23:30:00+08:00"), stamp("2025-11-01T01:00:00+08:00")),
            row("middle", stamp("2026-01-01T12:00:00+08:00"), stamp("2026-01-01T13:00:00+08:00")),
            row("right-edge", stamp("2026-10-31T23:30:00+08:00"), stamp("2026-11-01T00:30:00+08:00")),
            row("outside", stamp("2025-10-01T12:00:00+08:00"), stamp("2025-10-01T13:00:00+08:00")),
        ])
        result = reader.month("2026-10", scope="year")
        self.assertEqual(result["scope"], "year")
        self.assertEqual(result["range_start"], "2025-11-01")
        self.assertEqual(result["range_end"], "2026-11-01")
        self.assertEqual(len(result["days"]), 365)
        self.assertEqual(result["days"][0]["date"], "2025-11-01")
        self.assertEqual(result["days"][-1]["date"], "2026-10-31")
        self.assertEqual(result["summary"]["record_count"], 3)
        self.assertEqual(result["summary"]["categories"]["工作"]["record_count"], 3)
        self.assertEqual(result["summary"]["recorded_seconds"], 9000)
        self.assertEqual(self.transport.calls, 4)  # Schema + three queries, not twelve monthly scans.
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 4 * 1024 * 1024)

    def test_month_and_year_caches_do_not_mask_scope_changes_or_outages(self):
        reader = self.reader([
            row("january", stamp("2026-01-01T12:00:00+08:00"), stamp("2026-01-01T13:00:00+08:00")),
        ])
        month = reader.month("2026-10")
        self.assertEqual(month["scope"], "month")
        self.assertIsNone(month["summary"]["recorded_seconds"])
        month_calls = self.transport.calls
        year = reader.month("2026-10", scope="year")
        self.assertGreater(self.transport.calls, month_calls)
        self.assertEqual(year["summary"]["recorded_seconds"], 3600)
        self.assertEqual(reader.month("2026-10")["cache"]["state"], "cached")
        self.now += 60001
        self.transport.fail = True
        stale = reader.month("2026-10", scope="year")
        self.assertEqual(stale["scope"], "year")
        self.assertEqual(stale["range_start"], "2025-11-01")
        self.assertEqual(stale["cache"]["state"], "stale")
        self.assertEqual(stale["summary"]["recorded_seconds"], 3600)

    def test_old_month_only_cache_is_never_a_year_fallback(self):
        reader = self.reader([])
        cache_key = "desktop_heatmap_cache:" + digest(self.transport.table_key)[:24] + ":2026-10"
        self.store.set_setting(cache_key, {"month": "2026-10", "as_of_ms": self.now,
                                           "local_state_hash": "old-version", "days": [], "summary": {}})
        self.transport.fail = True
        with self.assertRaisesRegex(OSError, "synthetic_unavailable"):
            reader.month("2026-10", scope="year")
        fallback = reader.month("2026-10")
        self.assertEqual(fallback["scope"], "month")
        self.assertEqual(fallback["range_start"], "2026-10-01")
        self.assertEqual(fallback["range_end"], "2026-11-01")
        self.assertEqual(fallback["cache"]["state"], "stale")

    def test_cross_midnight_and_overlap_are_clipped_then_unioned(self):
        reader = self.reader([
            row("cross", stamp("2026-09-30T23:30:00+08:00"), stamp("2026-10-01T01:00:00+08:00")),
            row("overlap", stamp("2026-10-01T00:30:00+08:00"), stamp("2026-10-01T01:30:00+08:00")),
            row("month-edge", stamp("2026-10-31T23:30:00+08:00"), stamp("2026-11-01T01:00:00+08:00")),
        ])
        result = reader.month("2026-10")
        self.assertEqual(result["days"][0]["recorded_seconds"], 5400)
        self.assertEqual(result["days"][-1]["recorded_seconds"], 1800)
        self.assertEqual(result["summary"]["recorded_seconds"], 7200)
        self.assertEqual(result["summary"]["record_count"], 3)
        self.assertEqual(result["summary"]["categories"]["工作"]["record_count"], 3)
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertEqual(result["days"][0]["record_count"], 2)

    def test_categories_do_not_add_overlap_to_total(self):
        left, right = stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T10:00:00+08:00")
        result = self.reader([row("work", left, right), row("study", left, right,
                             **{"一级分类": ["学习"], "备注": "估算学习窗口"})]).month("2026-10")
        day = result["days"][0]
        self.assertEqual(day["recorded_seconds"], 3600)
        self.assertEqual(day["categories"]["工作"]["recorded_seconds"], 3600)
        self.assertEqual(day["categories"]["学习"]["recorded_seconds"], 3600)
        self.assertEqual(day["estimated_seconds"], 0)  # Fully covered by the confirmed work interval.
        self.assertEqual(day["estimated_count"], 1)
        self.assertEqual(day["categories"]["学习"]["estimated_seconds"], 3600)

    def test_unknown_boundaries_and_explicit_ai_window_are_not_heat(self):
        left, right = stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T10:00:00+08:00")
        result = self.reader([row("open", left),
                              row("ai", left, right, **{"时间花销": "AI 执行窗口", "备注": "[time-log-completion:2026-10-28] 估算窗口"}),
                              row("using-ai", left, right, **{"时间花销": "使用AI工具整理虚构资料"}),
                              row("checking-ai", left, right, **{"时间花销": "检查AI执行窗口"}),
                              row("not-ai-only", left, right, **{"时间花销": "非纯AI执行的虚构人工整理"})]).month("2026-10")
        day = result["days"][0]
        self.assertEqual(day["recorded_seconds"], 3600)
        self.assertEqual(day["record_count"], 4)
        self.assertEqual(day["unknown_count"], 1)
        self.assertEqual(day["excluded_ai_count"], 1)

    def test_saved_legacy_estimates_are_heat_but_not_confirmed_effort(self):
        left, right = stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T10:00:00+08:00")
        result = self.reader([row("estimate", left, right, **{"备注": "用户最新补报；结束采用报告时间估算"})]).month("2026-10")
        self.assertEqual(result["days"][0]["record_count"], 1)
        self.assertEqual(result["days"][0]["unknown_count"], 0)
        self.assertEqual(result["days"][0]["recorded_seconds"], 3600)
        self.assertEqual(result["days"][0]["estimated_seconds"], 3600)
        self.assertEqual(result["summary"]["estimated_count"], 1)
        self.assertIsNone(result["summary"]["confirmed_seconds"])
        self.assertIsNone(result["summary"]["human_seconds"])

    def test_estimates_clip_cross_midnight_and_deduplicate_confirmed_overlap(self):
        note = "[time-log-completion:2026-10-28]【估算补齐】依据已有切换节点；活动窗口非净人工"
        result = self.reader([
            row("sleep", stamp("2026-10-01T23:00:00+08:00"), stamp("2026-10-02T02:00:00+08:00"),
                **{"一级分类": ["休息"], "二级项目": ["睡觉"], "备注": note}),
            row("confirmed", stamp("2026-10-01T23:30:00+08:00"), stamp("2026-10-02T01:00:00+08:00"),
                **{"一级分类": ["休息"]}),
            row("second-estimate", stamp("2026-10-02T01:30:00+08:00"), stamp("2026-10-02T02:30:00+08:00"),
                **{"一级分类": ["休息"], "备注": "结束采用报告时间估算"}),
            row("tags-only", stamp("2026-10-02T03:00:00+08:00"), stamp("2026-10-02T04:00:00+08:00"),
                **{"备注": "未知人工时长；[time-log-tags:2026-10-28] 补现有分类"}),
        ]).month("2026-10")
        first, second = result["days"][:2]
        self.assertEqual((first["recorded_seconds"], first["estimated_seconds"]), (3600, 1800))
        # Day two has 00:00–02:30 plus 03:00–04:00 recorded. Of these,
        # 00:00–01:00 and 03:00–04:00 are confirmed, leaving 90 estimated minutes.
        self.assertEqual((second["recorded_seconds"], second["estimated_seconds"]), (12600, 5400))
        self.assertEqual(result["summary"]["recorded_seconds"], 16200)
        self.assertEqual(result["summary"]["confirmed_seconds"], 9000)
        self.assertEqual(result["summary"]["estimated_seconds"], 7200)
        self.assertEqual(result["summary"]["estimated_count"], 2)  # Unique activities, not daily counts.
        self.assertEqual(second["categories"]["休息"]["recorded_seconds"], 9000)
        self.assertEqual(second["categories"]["休息"]["estimated_seconds"], 5400)
        self.assertEqual(second["categories"]["工作"]["estimated_count"], 0)
        self.assertEqual(second["unknown_count"], 0)

    def test_estimate_annotation_never_fabricates_missing_or_invalid_boundaries(self):
        start, end = stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T10:00:00+08:00")
        note = "[time-log-completion:2026-10-28] 估算，报告时长30分钟"
        result = self.reader([row("start-only", start, **{"备注": note}),
                              row("end-only", end=end, **{"备注": note}),
                              row("reversed", end, start, **{"备注": note})]).month("2026-10")
        day = result["days"][0]
        self.assertIsNone(day["recorded_seconds"])
        self.assertEqual(day["unknown_count"], 3)
        self.assertEqual(day["estimated_seconds"], 0)
        self.assertEqual(day["estimated_count"], 0)

    def test_grouped_segments_keep_estimates_and_excluded_states_stay_out(self):
        start = stamp("2026-10-01T09:00:00+08:00")
        def segment(offset):
            return {"id": "activity:synthetic:1", "record_ids": [str(offset)], "source_refs": [],
                    "recorded_intervals": [], "human_intervals": [], "ai_intervals": [],
                    "heatmap_estimated_intervals": [[start + offset, start + offset + 3600000]]}
        items = _merge_heatmap_items([segment(0), segment(1800000)])
        totals = _totals(items, start, start + 7200000, {})
        self.assertEqual(totals["recorded_seconds"], 5400)
        self.assertEqual(totals["estimated_count"], 1)
        for status in ("recalled", "cancelled", "superseded"):
            with self.subTest(status=status):
                items[0]["status"] = status
                self.assertIsNone(_totals(items, start, start + 7200000, {})["recorded_seconds"])
        items[0]["status"], items[0]["role"] = "ended", "detail"
        self.assertIsNone(_totals(items, start, start + 7200000, {})["recorded_seconds"])

    def test_current_day_clips_future_and_empty_is_unknown(self):
        self.now = stamp("2026-10-01T10:00:00+08:00")
        result = self.reader([row("partial", stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T11:00:00+08:00"))]).month("2026-10")
        self.assertEqual(result["days"][0]["recorded_seconds"], 3600)
        self.assertTrue(result["days"][1]["future"])
        self.assertIsNone(result["days"][1]["recorded_seconds"])

    def test_complete_pagination_deduplicates_three_queries(self):
        left, right = stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T10:00:00+08:00")
        reader = self.reader([row("first", left, right), row("second", left, right)])
        self.transport.page_size = 1
        result = reader.month("2026-10")
        self.assertEqual(result["days"][0]["record_count"], 2)
        self.assertEqual(result["days"][0]["recorded_seconds"], 3600)

    def test_bad_pagination_is_not_cached(self):
        left, right = stamp("2026-10-01T09:00:00+08:00"), stamp("2026-10-01T10:00:00+08:00")
        reader = self.reader([row("first", left, right), row("second", left, right)])
        self.transport.page_size, self.transport.bad_pagination = 1, True
        with self.assertRaisesRegex(ValueError, "incomplete_timeline_pagination"):
            reader.month("2026-10")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM settings WHERE key LIKE 'desktop_heatmap_cache:%'").fetchone()[0], 0)

    def test_cache_is_bounded_and_outage_is_marked_stale(self):
        reader = self.reader([])
        first = reader.month("2026-10")
        calls = self.transport.calls
        second = reader.month("2026-10", refresh=True)
        self.assertEqual(self.transport.calls, calls)
        self.assertEqual(second["cache"]["state"], "cached")
        self.now += 60001
        self.transport.fail = True
        stale = reader.month("2026-10")
        self.assertEqual(stale["cache"]["state"], "stale")
        self.assertEqual(stale["as_of_ms"], first["as_of_ms"])
        self.assertEqual(stale["cache"]["refresh_error"], "OSError")


if __name__ == "__main__":
    unittest.main()
