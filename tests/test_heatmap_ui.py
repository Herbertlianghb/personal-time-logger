import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from time_logger.desktop_controller import DesktopController
from time_logger.heatmap_charts import _daily_tip
from time_logger.heatmap_ui import HeatmapDialog

APP = QApplication.instance() or QApplication([])


def snapshot(month="2001-01", seconds=3600):
    return {"month": month, "as_of_ms": 978368400000, "completeness": "partial",
            "cache": {"state": "fresh"}, "limitations": ["synthetic test data"],
            "summary": {"record_count": 3, "unknown_count": 2,
                        "categories": {"工作": {"record_count": 1, "unknown_count": 0}}},
            "days": [{"date": month + "-01", "recorded_seconds": seconds,
                      "record_count": 2, "unknown_count": 1,
                      "categories": {"工作": {"recorded_seconds": 1800, "record_count": 1, "unknown_count": 0}}},
                     {"date": month + "-02", "recorded_seconds": None,
                      "record_count": 1, "unknown_count": 1, "categories": {}}]}


class HeatmapUiTests(unittest.TestCase):
    def setUp(self):
        self.dialog = HeatmapDialog("2001-01-01", scope="month")
        self.addCleanup(self.dialog.close)

    def test_unread_does_not_claim_zero_hours(self):
        self.assertIn("尚未读取", self.dialog.summary.text())
        self.assertEqual(self.dialog.card_values["total"].text(), "—")

    def test_filter_uses_category_union_and_retains_unknown_entry(self):
        self.dialog.receive_snapshot(snapshot())
        self.assertIn("1小时", self.dialog.card_values["total"].text())
        self.assertIn("起止不全 2", self.dialog.summary.text())
        self.dialog.set_category("工作")
        self.assertIn("30分钟", self.dialog.card_values["total"].text())
        self.assertIsNone(self.dialog.statistics["daily"][1]["seconds"])
        self.assertIn("数据有缺口", self.dialog.status.text())

    def test_day_selection_reuses_day_history_date(self):
        values = []
        self.dialog.day_selected.connect(values.append)
        self.dialog.receive_snapshot(snapshot())
        self.dialog.choose_day(0)
        self.assertEqual(values, ["2001-01-01"])

    def test_stale_response_for_previous_month_is_ignored(self):
        months = []
        self.dialog.month_requested.connect(lambda month, refresh: months.append((month, refresh)))
        self.dialog.change_month(1)
        self.dialog.receive_snapshot(snapshot())
        self.assertEqual(self.dialog.snapshot, {})
        self.assertEqual(months, [("2001-02", False)])

    def test_daily_weekly_and_cumulative_switch_without_another_request(self):
        requests = []
        self.dialog.month_requested.connect(lambda *args: requests.append(args))
        self.dialog.receive_snapshot(snapshot())
        for mode in ("weekly", "cumulative", "daily"):
            self.dialog.set_mode(mode)
            self.assertEqual(self.dialog.chart.mode, mode)
            self.assertIn("1小时", self.dialog.card_values["total"].text())
        self.assertEqual(requests, [])

    def test_estimate_labels_remain_consistent_across_modes_and_category_filter(self):
        source = snapshot()
        source["days"][0].update(estimated_seconds=1800, estimated_count=1)
        source["summary"]["estimated_count"] = 1
        self.dialog.receive_snapshot(source)
        for mode in ("daily", "weekly", "cumulative"):
            self.dialog.set_mode(mode)
            self.assertTrue(self.dialog.card_values["total"].text().startswith("约 "))
            self.assertTrue(self.dialog.card_values["average"].text().startswith("约 "))
            self.assertTrue(self.dialog.card_values["peak"].text().startswith("约 "))
            self.assertIn("含估算 1 条", self.dialog.summary.text())
            self.assertIn("估算", self.dialog.legend.text())
            self.assertEqual(self.dialog.statistics["estimated_seconds"], 1800)
        self.dialog.set_category("工作")
        self.assertFalse(self.dialog.card_values["total"].text().startswith("约 "))
        self.assertIn("含估算 0 条", self.dialog.summary.text())

    def test_hover_distinguishes_estimate_unknown_records_and_no_records(self):
        day = {"date": "2001-01-01", "seconds": 3600, "estimated_seconds": 1800,
               "estimated_count": 1, "record_count": 1, "unknown_count": 0}
        self.assertIn("约 1小时", _daily_tip(day))
        self.assertIn("含估算区间 30分钟", _daily_tip(day))
        self.assertIn("不等于净人工", _daily_tip(day))
        day.update(estimated_seconds=0)
        self.assertIn("已被确定区间覆盖", _daily_tip(day))
        day.update(seconds=None, estimated_count=0, unknown_count=1)
        self.assertIn("有记录，起止待补", _daily_tip(day))
        day.update(record_count=0, unknown_count=0)
        self.assertIn("无记录", _daily_tip(day))

    def test_range_changes_clear_old_summary_and_reject_month_payload(self):
        self.dialog.receive_snapshot(snapshot())
        self.dialog.scope_combo.setCurrentIndex(0)
        self.assertEqual(self.dialog.scope, "year")
        self.assertEqual(self.dialog.snapshot, {})
        self.dialog.receive_snapshot(snapshot())
        self.assertEqual(self.dialog.snapshot, {})
        self.assertEqual(self.dialog.card_values["total"].text(), "—")


class HeatmapControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.response = snapshot()
        outer = self

        class Transport:
            calls = []
            def request(self, op, **fields):
                self.calls.append((op, fields))
                return outer.response
            def close(self):
                pass

        self.controller = DesktopController({}, Path(temp.name) / "cache.sqlite3", Transport())
        self.addCleanup(self.controller.stop)

    def test_snapshot_caches_only_success_and_never_changes_draft(self):
        self.controller.set_draft("unsent draft")
        self.controller.request_heatmap("2001-01")
        self.controller._refresh_heatmap()
        self.assertEqual(self.controller.local.preference("heatmap:2001-01"), snapshot())
        self.response = {**snapshot(seconds=None), "cache": {"state": "stale"}}
        self.controller.request_heatmap("2001-01", refresh=True)
        self.controller._refresh_heatmap()
        self.assertEqual(self.controller.local.preference("heatmap:2001-01"), snapshot())
        self.assertEqual(self.controller.get_draft(), "unsent draft")

    def test_wrong_month_response_does_not_replace_cache(self):
        self.controller.request_heatmap("2001-02")
        with self.assertRaisesRegex(ValueError, "heatmap_month_mismatch"):
            self.controller._refresh_heatmap()
        self.assertIsNone(self.controller.local.preference("heatmap:2001-02"))

    def test_year_request_rejects_old_server_month_response(self):
        self.controller.request_heatmap("2001-01", scope="year")
        with self.assertRaisesRegex(ValueError, "heatmap_scope_not_supported"):
            self.controller._refresh_heatmap()
        self.assertIsNone(self.controller.local.preference("heatmap:year:2001-01"))

    def test_month_and_year_caches_are_separate(self):
        self.controller.request_heatmap("2001-01")
        self.controller._refresh_heatmap()
        self.response = {**snapshot(), "scope": "year", "range_start": "2000-02-01", "range_end": "2001-02-01"}
        self.controller.request_heatmap("2001-01", scope="year")
        self.controller._refresh_heatmap()
        self.assertEqual(self.controller.local.preference("heatmap:2001-01"), snapshot())
        self.assertEqual(self.controller.local.preference("heatmap:year:2001-01"), self.response)


if __name__ == "__main__":
    unittest.main()
