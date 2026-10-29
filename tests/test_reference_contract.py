"""Independent behavior checks for the user-approved proportional UI.

All times and text below are synthetic fixtures, never submitted to a backend.
"""
import copy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QInputMethodEvent, QKeyEvent, QPainter
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication

from time_logger.desktop_ui import RecordEdit
from time_logger.timeline_widget import TimelineWidget
import time_logger.timeline_widget as timeline_module


APP = QApplication.instance() or QApplication([])
LEFT = int(datetime(2026, 10, 25, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
MINUTE = 60000


def snapshot(items, elapsed=60):
    return {"date": "2026-10-25", "timezone": "Asia/Taipei", "day_start_ms": LEFT,
            "day_end_ms": LEFT + 86400000, "display_end_ms": LEFT + elapsed * MINUTE,
            "as_of_ms": LEFT + elapsed * MINUTE, "activities": items,
            "summary": {"human_seconds": None, "recorded_seconds": None,
                        "ai_seconds": None, "completeness": "partial"}}


class ProportionalDisplayContract(unittest.TestCase):
    def setUp(self):
        self.widget = TimelineWidget()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()

    def test_ai_and_report_point_do_not_fill_an_unrecorded_human_gap(self):
        value = snapshot([
            {"id": "human", "name": "fixture activity", "start_ms": LEFT,
             "end_ms": LEFT + 30 * MINUTE, "category": "工作",
             "recorded_intervals": [[LEFT, LEFT + 10 * MINUTE],
                                    [LEFT + 20 * MINUTE, LEFT + 30 * MINUTE]],
             "human_intervals": [[LEFT, LEFT + 10 * MINUTE]], "ai_intervals": []},
            {"id": "ai", "name": "fixture AI", "actor": "ai",
             "start_ms": LEFT + 10 * MINUTE, "end_ms": LEFT + 20 * MINUTE,
             "recorded_intervals": [], "human_intervals": [],
             "ai_intervals": [[LEFT + 10 * MINUTE, LEFT + 20 * MINUTE]]},
            {"id": "report", "name": "fixture report", "report_ms": LEFT + 15 * MINUTE,
             "reported_duration_seconds": 300, "estimated": True},
        ])
        original = copy.deepcopy(value)
        self.widget.set_snapshot(value)
        pieces = [(gap["start_ms"], gap["end_ms"]) for gap in self.widget.gaps]
        self.assertIn((LEFT + 10 * MINUTE, LEFT + 20 * MINUTE), pieces)
        self.assertEqual(value, original)
        # A record's outer start/end must not fill its explicitly missing middle.
        self.assertFalse(any(bar["kind"] != "ai" and bar["start_ms"] < LEFT + 15 * MINUTE < bar["end_ms"]
                             for bar in self.widget.bars))

    def test_explicit_outer_window_is_visible_but_not_promoted_to_human_effort(self):
        start, end = LEFT + 5 * MINUTE, LEFT + 15 * MINUTE
        value = snapshot([{"id": "window", "name": "fixture explicit window",
                           "start_ms": start, "end_ms": end, "actor": "human", "role": "primary",
                           "status": "ended", "category": "工作", "formal_state": "verified_ptl",
                           "recorded_intervals": [], "human_intervals": [], "ai_intervals": []}])
        original = copy.deepcopy(value)
        self.widget.set_snapshot(value)
        windows = [bar for bar in self.widget.bars if bar["kind"] == "window"]
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0]["start_ms"], windows[0]["end_ms"]), (start, end))
        self.assertEqual(windows[0]["height"], 10 * self.widget.scale.pixels_per_minute)
        self.assertFalse(any(bar["kind"] == "human" for bar in self.widget.bars))
        self.assertEqual(value, original)
        self.assertIsNone(value["summary"]["human_seconds"])

    def test_coincident_end_start_and_report_all_remain_selectable(self):
        point = LEFT + 5 * MINUTE
        value = snapshot([
            {"id": "ending", "name": "fixture ending", "start_ms": LEFT, "end_ms": point},
            {"id": "starting", "name": "fixture starting", "start_ms": point,
             "end_ms": LEFT + 15 * MINUTE},
            {"id": "report", "name": "fixture report", "report_ms": point},
        ])
        self.widget.set_snapshot(value)
        at_point = [event for event in self.widget.events() if event["timestamp"] == point]
        self.assertEqual({event["item"]["id"] for event in at_point}, {"ending", "starting", "report"})
        self.assertEqual(len({event["key"] for event in at_point}), 3)
        self.assertEqual(len({event["y"] for event in at_point}), 1)
        before = [(event["key"], event["timestamp"], event["y"]) for event in self.widget.events()]
        for event in at_point:
            self.widget.select_event(event["key"])
            self.assertEqual(self.widget.selected_key, event["key"])
        self.assertEqual(before, [(event["key"], event["timestamp"], event["y"]) for event in self.widget.events()])

    def test_future_cutoff_never_becomes_a_claimed_end_event(self):
        value = snapshot([{"id": "future-end", "name": "fixture partial window",
                           "start_ms": LEFT + 20 * MINUTE, "end_ms": LEFT + 50 * MINUTE,
                           "recorded_intervals": [[LEFT + 20 * MINUTE, LEFT + 50 * MINUTE]],
                           "human_intervals": [], "ai_intervals": []}], elapsed=30)
        self.widget.set_snapshot(value)
        self.assertTrue(all(bar["end_ms"] <= LEFT + 30 * MINUTE for bar in self.widget.bars))
        self.assertTrue(all(gap["end_ms"] <= LEFT + 30 * MINUTE for gap in self.widget.gaps))
        events = self.widget.events()
        self.assertFalse(any(event["timestamp"] > LEFT + 30 * MINUTE for event in events))
        self.assertFalse(any(event["kind"] == "end" and event["timestamp"] == LEFT + 30 * MINUTE for event in events))

    def test_cross_day_continuation_does_not_relabel_the_original_start(self):
        value = snapshot([{"id": "cross", "name": "fixture cross day", "start_ms": LEFT - 30 * MINUTE,
                           "end_ms": LEFT + 10 * MINUTE,
                           "recorded_intervals": [[LEFT, LEFT + 10 * MINUTE]],
                           "human_intervals": [], "ai_intervals": []}])
        original = copy.deepcopy(value)
        self.widget.set_snapshot(value)
        at_left = [event for event in self.widget.events() if event["timestamp"] == LEFT]
        self.assertTrue(at_left)
        self.assertTrue(any("延续" in event["title"] or event["kind"] in {"continuation", "continue"} for event in at_left))
        self.assertEqual(value, original)

    def test_window_and_parallel_ai_captions_do_not_overprint(self):
        start, end = LEFT + 15 * MINUTE, LEFT + 45 * MINUTE
        items = [{"id": "window", "name": "fixture window", "actor": "human", "role": "primary",
                  "start_ms": start, "end_ms": end, "recorded_intervals": [],
                  "human_intervals": [], "ai_intervals": []}]
        for number in (1, 2):
            items.append({"id": f"ai{number}", "name": f"fixture AI {number}", "actor": "ai",
                          "start_ms": start, "end_ms": end, "recorded_intervals": [],
                          "human_intervals": [], "ai_intervals": [[start, end]]})
        value = snapshot(items)
        original = copy.deepcopy(value)
        self.widget.set_snapshot(value)
        self.widget.setFixedWidth(390)
        geometry = [(bar["kind"], bar["start_ms"], bar["end_ms"], bar["y"], bar["height"])
                    for bar in self.widget.bars]
        texts, hatches = [], []

        class RecordingPainter(QPainter):
            def drawText(self, *args):
                texts.append(args[-1])
                return super().drawText(*args)

            def fillRect(self, *args):
                if isinstance(args[-1], QBrush) and args[-1].style() == Qt.BrushStyle.BDiagPattern:
                    hatches.append(args[0])
                return super().fillRect(*args)

        with patch.object(timeline_module, "QPainter", RecordingPainter):
            rendered = self.widget.grab()
        # The window's explanation wins the shared location; the missing
        # interval remains hatched and is never counted as confirmed effort.
        self.assertIn("fixture window", texts)
        self.assertIn("活动窗口 · 人工未知", texts)
        self.assertNotIn("时间信息待补充", texts)
        self.assertEqual(texts.count("30分钟"), 1)
        self.assertEqual(len(hatches), 1)
        self.assertEqual((hatches[0].top(), hatches[0].height()),
                         (self.widget.scale.y(LEFT), self.widget.scale.height(LEFT, LEFT + 60 * MINUTE)))
        self.assertEqual(geometry, [(bar["kind"], bar["start_ms"], bar["end_ms"], bar["y"], bar["height"])
                                    for bar in self.widget.bars])
        self.assertEqual(len(self.widget.bars), 3)
        self.assertEqual(len(self.widget.events()), 6)
        self.assertEqual({event["item"]["id"] for event in self.widget.events()}, {"window", "ai1", "ai2"})
        self.assertEqual(value, original)
        if os.environ.get("PTL_CAPTION_REVIEW_IMAGE"):
            self.assertTrue(rendered.save(os.environ["PTL_CAPTION_REVIEW_IMAGE"]))


class EnterInputContract(unittest.TestCase):
    def setUp(self):
        self.editor = RecordEdit()
        self.editor.show()
        self.editor.setFocus()
        APP.processEvents()
        self.sent = QSignalSpy(self.editor.submit_requested)

    def tearDown(self):
        self.editor.close()
        self.editor.deleteLater()

    def test_enter_submits_ctrl_enter_only_inserts_newline(self):
        self.editor.setPlainText("fixture text")
        QTest.keyClick(self.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.sent.count(), 0)
        self.assertIn("\n", self.editor.toPlainText())
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertEqual(self.sent.count(), 1)

    def test_ime_commit_then_enter_is_not_sent_until_a_new_key_press(self):
        APP.sendEvent(self.editor, QInputMethodEvent("zhong", []))
        committed = QInputMethodEvent("", [])
        committed.setCommitString("中")
        APP.sendEvent(self.editor, committed)
        QTest.keyPress(self.editor, Qt.Key.Key_Return)
        self.assertEqual(self.sent.count(), 0)
        QTest.keyRelease(self.editor, Qt.Key.Key_Return)
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertEqual(self.sent.count(), 1)

    def test_auto_repeat_enter_never_creates_an_extra_submission(self):
        self.editor.setPlainText("fixture repeat")
        repeated = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                            Qt.KeyboardModifier.NoModifier, "\r", True, 1)
        APP.sendEvent(self.editor, repeated)
        self.assertEqual(self.sent.count(), 0)


if __name__ == "__main__":
    unittest.main()
