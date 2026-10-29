"""Rendering contracts for clock readability; all inputs are synthetic."""
import copy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication

import time_logger.timeline_widget as timeline_module
from time_logger.timeline_widget import TimelineWidget, duration

APP = QApplication.instance() or QApplication([])
LEFT = int(datetime(2026, 10, 28, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
MINUTE = 60000


class TimelineReadabilityTests(unittest.TestCase):
    def setUp(self):
        self.widget = TimelineWidget()
        self.widget.setFixedWidth(390)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()

    def render(self):
        texts, grid = [], []

        class RecordingPainter(QPainter):
            def drawText(self, *args):
                texts.append((args[0], args[-1], self.pen().color().name()))
                return super().drawText(*args)

            def drawLine(self, *args):
                if len(args) == 2 and isinstance(args[0], QPointF) and args[0].x() == 54:
                    grid.append(args[0].y())
                return super().drawLine(*args)

        with patch.object(timeline_module, "QPainter", RecordingPainter):
            self.widget.grab()
        return texts, grid

    def test_overview_has_hourly_guides_and_real_times_at_original_positions(self):
        value = {"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                 "display_end_ms": LEFT + 120 * MINUTE, "activities": [
                     {"id": "first", "name": "first", "start_ms": LEFT + 3 * MINUTE},
                     {"id": "dense", "name": "dense", "start_ms": LEFT + 7 * MINUTE},
                     {"id": "later", "name": "later", "start_ms": LEFT + 72 * MINUTE}]}
        original = copy.deepcopy(value)
        self.widget.set_snapshot(value)
        before = [(event["key"], event["timestamp"], event["y"]) for event in self.widget.events()]
        texts, grid = self.render()
        self.assertEqual(grid, [self.widget.scale.y(LEFT + n * MINUTE) for n in (0, 60, 120)])
        actual = {text: point for point, text, color in texts if color == "#295b50" and ":" in text}
        self.assertIn("00:03", actual)
        self.assertEqual(actual["00:03"].y(), self.widget.scale.y(LEFT + 3 * MINUTE) + 4)
        self.assertIn("01:12", actual)
        self.assertEqual(value, original)
        self.assertEqual(before, [(event["key"], event["timestamp"], event["y"]) for event in self.widget.events()])

    def test_selected_dense_time_wins_without_moving_any_event(self):
        self.widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                                  "display_end_ms": LEFT + 60 * MINUTE, "activities": [
                                      {"id": "a", "start_ms": LEFT + 3 * MINUTE},
                                      {"id": "b", "start_ms": LEFT + 7 * MINUTE}]})
        before = [(event["timestamp"], event["y"]) for event in self.widget.events()]
        self.widget.select_event(self.widget.events()[1]["key"])
        texts, _ = self.render()
        actual = [text for _, text, color in texts if color == "#295b50" and ":" in text]
        self.assertIn("00:07", actual)
        self.assertNotIn("00:03", actual)
        self.assertEqual(before, [(event["timestamp"], event["y"]) for event in self.widget.events()])
        self.widget.set_zoom(8)
        _, grid = self.render()
        self.assertEqual(grid, [self.widget.scale.y(LEFT + n * MINUTE) for n in (0, 15, 30, 45, 60)])
        self.assertEqual(self.widget.events()[1]["y"] - self.widget.events()[0]["y"], 4 * 8)

    def test_unknown_span_has_no_false_activity_duration_caption(self):
        end = LEFT + 7 * 3600000 + 55.2286 * MINUTE
        self.widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                                  "display_end_ms": int(end), "activities": [
                                      {"id": "first", "report_ms": LEFT},
                                      {"id": "report", "report_ms": int(end)}]})
        original = [(gap["start_ms"], gap["end_ms"], gap["height"]) for gap in self.widget.gaps]
        texts, _ = self.render()
        captions = [text for _, text, _ in texts]
        self.assertIn("时间信息待补充", captions)
        self.assertFalse(any("小时" in text or "55.228" in text for text in captions))
        self.assertEqual(original, [(gap["start_ms"], gap["end_ms"], gap["height"]) for gap in self.widget.gaps])

    def test_duration_limits_display_precision_without_changing_input(self):
        self.assertEqual(duration(None), "未知")
        self.assertEqual(duration(1200), "20分钟")
        self.assertEqual(duration(7 * 3600 + 55.2286 * 60), "约7小时55分")
        self.assertEqual(duration(3599.8), "约1小时")
        self.assertEqual(duration(6.123456), "约6秒")
        self.assertEqual(duration(0.1), "不足1秒")

    def test_report_sleep_overlay_is_visible_but_never_confirmed_human(self):
        start, end = LEFT + 62 * MINUTE + 32000, LEFT + 349 * MINUTE + 57000
        estimate = {"start_ms": start, "end_ms": end, "kind": "sleep_report_window",
                    "label": "睡眠 · 依据报告估计", "uncertainty": "报告估计，非实测，不计主动人工。",
                    "source_refs": [{"id": "sleep-report", "report_ms": start, "role": "sleep_report"},
                                    {"id": "wake-report", "report_ms": end, "role": "wake_report"}]}
        value = {"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                 "display_end_ms": LEFT + 360 * MINUTE, "summary": {"human_seconds": None},
                 "activities": [{"id": "sleep", "name": "sleep", "start_ms": None, "end_ms": None,
                                 "report_ms": start, "estimated_intervals": [estimate],
                                 "recorded_intervals": [], "human_intervals": [], "ai_intervals": []}]}
        original = copy.deepcopy(value)
        self.widget.set_snapshot(value)
        bars = [bar for bar in self.widget.bars if bar["kind"] == "sleep_estimate"]
        self.assertEqual(len(bars), 1)
        self.assertEqual((bars[0]["start_ms"], bars[0]["end_ms"]), (start, end))
        self.assertEqual(bars[0]["height"], self.widget.scale.height(start, end))
        self.assertFalse(any(gap["start_ms"] < end and gap["end_ms"] > start for gap in self.widget.gaps))
        endpoints = [event for event in self.widget.events() if event["kind"].startswith("estimated_")]
        self.assertEqual({event["timestamp"] for event in endpoints}, {start, end})
        self.assertTrue(all("估计" in event["title"] for event in endpoints))
        self.assertTrue(all(event["item"]["display_estimate"]["source_refs"] == estimate["source_refs"] for event in endpoints))
        texts, _ = self.render()
        self.assertIn("睡眠（约）", [text for _, text, _ in texts])
        self.assertIn("约4小时47分", [text for _, text, _ in texts])
        self.assertEqual(value, original)
        self.assertIsNone(value["summary"]["human_seconds"])

    def test_overnight_without_server_estimate_stays_unknown(self):
        self.widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                                  "display_end_ms": LEFT + 8 * 3600000, "activities": [
                                      {"id": "a", "report_ms": LEFT, "name": "sleep?"},
                                      {"id": "b", "report_ms": LEFT + 8 * 3600000, "name": "wake?"}]})
        self.assertFalse(self.widget.bars)
        self.assertEqual([(gap["start_ms"], gap["end_ms"]) for gap in self.widget.gaps],
                         [(LEFT, LEFT + 8 * 3600000)])


if __name__ == "__main__":
    unittest.main()
