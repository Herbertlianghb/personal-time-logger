"""Synthetic Feishu target periods and outage cache; no real account access."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from time_logger.base_adapter import LarkCliTransport
from time_logger.daily_goals import DailyGoalReader, FIELDS
from time_logger.heatmap import monthly_heatmap
from time_logger.store import Store
from time_logger.timeline import _timestamp


def standard(rid, start, end, work, study=None):
    return {"record_id": rid, "fields": {"时段": rid, "开始时间": start, "结束时间": end,
            "工作时间标准（h）": work, "学习时间标准（h）": study}}


class GoalTransport:
    _records = staticmethod(LarkCliTransport._records)
    table_key = ("synthetic", "base", "standards")

    def __init__(self, rows):
        self.rows = rows
        self.calls, self.fail, self.fields = 0, False, FIELDS.copy()

    def field_schema(self):
        self.calls += 1
        if self.fail:
            raise OSError("synthetic_offline")
        return {"fields": [{"name": name, "type": kind} for name, kind in self.fields.items()]}

    def _call(self, command, *args):
        assert command == "+record-list"
        names = list(FIELDS)
        return {"fields": names, "record_id_list": [r["record_id"] for r in self.rows],
                "data": [[r["fields"].get(n) for n in names] for r in self.rows], "has_more": False}


class DailyGoalReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3", "test", ["self"])
        self.now = _timestamp("2026-10-31T10:00:00+08:00")
        self.transport = GoalTransport([
            standard("previous", "2026-01-01", "2026-10-30", 12),
            standard("current", "2026-10-31", "2026-12-31", 8, 3)])
        self.reader = DailyGoalReader(self.store, self.transport, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_current_and_historical_periods_are_separate_and_inclusive(self):
        self.assertEqual(self.reader.read("2026-10-30")["work_hours"], 12)
        current = self.reader.read("2026-10-31")
        self.assertEqual((current["work_hours"], current["study_hours"]), (8, 3))
        self.assertEqual(self.reader.read("2026-12-31")["study_hours"], 3)
        self.assertIsNone(self.reader.read("2027-01-01")["work_hours"])

    def test_goal_change_is_read_after_bounded_cache_expiry(self):
        self.reader.read("2026-10-31")
        self.transport.rows[-1]["fields"]["工作时间标准（h）"] = 9
        self.now += 59000
        self.assertEqual(self.reader.read("2026-10-31")["work_hours"], 8)
        self.now += 2000
        self.assertEqual(self.reader.read("2026-10-31")["work_hours"], 9)

    def test_outage_keeps_last_feishu_target_and_marks_it_stale(self):
        self.reader.read("2026-10-31")
        self.now += 61000
        self.transport.fail = True
        value = self.reader.read("2026-10-31")
        self.assertEqual((value["work_hours"], value["study_hours"]), (8, 3))
        self.assertEqual(value["cache"]["state"], "stale")
        self.assertIsNone(self.reader.read("2026-11-01")["work_hours"])

    def test_overlapping_periods_fail_closed_without_picking_one(self):
        self.transport.rows[0]["fields"]["结束时间"] = "2026-10-31"
        value = self.reader.read("2026-10-31")
        self.assertIsNone(value["work_hours"])
        self.assertEqual(value["cache"]["state"], "unavailable")

    def test_missing_field_or_invalid_hour_never_becomes_default(self):
        self.transport.fields.pop("学习时间标准（h）")
        self.assertEqual(self.reader.read("2026-10-31")["cache"]["state"], "unavailable")
        self.transport.fields = FIELDS.copy()
        self.transport.rows[-1]["fields"]["工作时间标准（h）"] = 25
        self.assertIsNone(self.reader.read("2026-10-31")["work_hours"])

    def test_monthly_response_reads_current_targets_even_for_old_month(self):
        config = {"app_id": "test", "app_secret": "test", "base_token": "synthetic",
                  "time_table_id": "time", "time_standards_table_id": "standards"}
        with patch("time_logger.feishu.Feishu"), patch("time_logger.heatmap.HttpBaseTransport"), \
             patch("time_logger.heatmap.HeatmapReader") as heat, \
             patch("time_logger.daily_goals.DailyGoalReader") as goals:
            heat.return_value.month.return_value = {"server_now_ms": self.now}
            goals.return_value.read.return_value = {"work_hours": 8, "study_hours": 3}
            result = monthly_heatmap(self.store, config, "2026-01")
            goals.return_value.read.assert_called_once_with("2026-10-31")
            self.assertEqual(result["daily_goals"]["study_hours"], 3)


if __name__ == "__main__":
    unittest.main()
