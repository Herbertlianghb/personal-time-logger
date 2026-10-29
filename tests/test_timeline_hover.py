import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QEnterEvent, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget

from time_logger.timeline_hover import TimelineHoverCard


APP = QApplication.instance() or QApplication([])


class TimelineHoverTests(unittest.TestCase):
    def setUp(self):
        self.parent = QWidget()
        self.card = TimelineHoverCard(self.parent)
        self.tag = QRect(30, 60, 64, 20)

    def tearDown(self):
        self.card.dismiss()
        self.parent.close()
        self.parent.deleteLater()
        APP.processEvents()

    def show(self, items=None, rect=None):
        self.card.show_records(QPoint(50, 70), items or [{"name": "刷单词", "category": "学习", "project": "英语"}], rect or self.tag)
        APP.processEvents()

    def texts(self):
        return [label.text() for label in self.card.findChildren(QLabel)]

    def test_project_and_category_are_distinct_and_content_is_plain_text(self):
        self.show([{"name": "<b>刷单词</b>", "category": "学习", "project": None}])
        self.assertIn("<b>刷单词</b>", self.texts())
        self.assertIn("一级分类  学习", self.texts())
        self.assertIn("二级项目  未指定", self.texts())
        self.assertNotIn("二级项目  <b>刷单词</b>", self.texts())
        self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in self.card.findChildren(QLabel)))

    def test_report_timestamp_does_not_become_an_actual_start(self):
        self.show([{"name": "生活记录", "category": "生活", "report_ms": 1793137797224}])
        self.assertIn("报告时刻  10-28 05:49 · 实际起止未定", self.texts())
        self.assertFalse(any(text.startswith("记录边界") for text in self.texts()))

    def test_sleep_window_remains_explicit_estimate(self):
        self.show([{"name": "睡觉休息", "category": "休息", "estimated_intervals": [{
            "kind": "sleep_report_window", "start_ms": 1793120552484, "end_ms": 1793137797224,
        }]}])
        self.assertIn("睡眠（约）  10-28 01:02 → 10-28 05:49", self.texts())
        self.assertIn("依据睡前与醒来报告估计，非实测睡眠，不计主动人工投入。", self.texts())
        self.assertIn("实际起止未定", self.texts())

    def test_cluster_is_bounded_and_preserves_total_count_hint(self):
        self.show([{"name": "活动" + str(index), "project": "项目" + str(index)} for index in range(9)])
        self.assertEqual(self.card.heading.text(), "具体记录 · 共 9 条")
        self.assertIn("另有 5 条，请在“查看节点”中选择。", self.texts())
        self.assertIn("活动3", self.texts())
        self.assertNotIn("活动4", self.texts())
        self.assertGreater(self.card.scroll.height(), 0)

    def test_card_clamps_to_available_screen_and_does_not_accept_focus(self):
        self.show(rect=QRect(99999, 99999, 60, 20))
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.assertTrue(screen.contains(self.card.frameGeometry()))
        self.assertTrue(self.card.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(self.card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(self.card.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_delayed_leave_keeps_card_when_pointer_reaches_card(self):
        self.show()
        with patch("time_logger.timeline_hover.QCursor.pos", return_value=self.card.frameGeometry().center()):
            self.card.schedule_hide()
            QTest.qWait(180)
        self.assertTrue(self.card.isVisible())
        with patch("time_logger.timeline_hover.QCursor.pos", return_value=QPoint(-100, -100)):
            APP.sendEvent(self.card, QEvent(QEvent.Type.Leave))
            QTest.qWait(180)
        self.assertFalse(self.card.isVisible())

    def test_tag_entry_and_card_entry_cancel_delayed_leave(self):
        self.show()
        with patch("time_logger.timeline_hover.QCursor.pos", return_value=self.tag.center()):
            self.card.schedule_hide()
            QTest.qWait(180)
        self.assertTrue(self.card.isVisible())
        self.card.schedule_hide()
        APP.sendEvent(self.card, QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(self.card.pos() + QPoint(10, 10))))
        self.assertFalse(self.card._hide_timer.isActive())
        self.card.dismiss()
        self.assertFalse(self.card.isVisible())
        self.assertFalse(self.card._hide_timer.isActive())

    def test_empty_items_dismisses_old_record(self):
        self.show()
        self.card.show_records(QPoint(), [], self.tag)
        self.assertFalse(self.card.isVisible())


if __name__ == "__main__":
    unittest.main()
