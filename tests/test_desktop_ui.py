import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal, Qt, QSettings, QDate, QPoint
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication

from time_logger.desktop_ui import ActivityDialog, DesktopWindow, RecordEdit
from time_logger.timeline_widget import TimeScale, TimelineWidget, label_groups, VISIBLE_CATEGORIES


APP = QApplication.instance() or QApplication([])
TZ = timezone(timedelta(hours=8))
LEFT = int(datetime(2026, 10, 26, tzinfo=TZ).timestamp() * 1000)


class FakeController(QObject):
    snapshot = Signal(dict)
    state = Signal(str)
    pending = Signal(list)
    questions = Signal(list)
    sync_busy = Signal(bool)

    def __init__(self):
        super().__init__()
        self.draft = ""
        self.calls = []
        self.fail = False
        self.visible = None
        self.retry_calls = 0
        self.retry_fail = False

    def get_draft(self):
        return self.draft

    def set_draft(self, value):
        if self.fail:
            raise OSError("read_only")
        self.draft = value

    def submit(self, text, **kwargs):
        if self.fail:
            raise OSError("read_only")
        self.calls.append((text, kwargs))
        return True

    def refresh(self, day):
        pass

    def load_cached(self, day):
        pass

    def set_visible(self, value):
        self.visible = value

    def retry_sync(self):
        self.retry_calls += 1
        if self.retry_fail:
            raise OSError("unavailable")
        self.sync_busy.emit(True)
        return True


class GeometryTests(unittest.TestCase):
    def test_six_category_tags_cluster_without_moving_or_dropping_source_events(self):
        widget = TimelineWidget()
        widget.resize(390, 200)
        items = [{"id": "work1", "name": "record one", "category": "工作", "start_ms": LEFT},
                 {"id": "work2", "name": "record two", "category": "工作", "start_ms": LEFT + 60000},
                 {"id": "income", "category": "项目收入", "start_ms": LEFT + 120000},
                 {"id": "unknown", "category": None, "start_ms": LEFT + 180000}]
        for i, category in enumerate(VISIBLE_CATEGORIES[1:], 1):
            items.append({"id": category, "category": category, "start_ms": LEFT + i * 30 * 60000})
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                             "display_end_ms": LEFT + 180 * 60000, "activities": items})
        before = [(a["key"], a["timestamp"], a["y"]) for a in widget.events()]
        widget.show()
        widget.grab()
        self.assertEqual({tag["category"] for tag in widget.tags}, set(VISIBLE_CATEGORIES))
        merged = next(tag for tag in widget.tags if tag["category"] == "工作")
        self.assertEqual({item["id"] for item in merged["items"]}, {"work1", "work2"})
        self.assertEqual(merged["rect"].center().y(), widget.scale.y(LEFT))
        self.assertIn("2", merged["text"])
        self.assertEqual(before, [(a["key"], a["timestamp"], a["y"]) for a in widget.events()])
        self.assertEqual({a["item"]["id"] for a in widget.events()}, {item["id"] for item in items})
        hovered = QSignalSpy(widget.tag_hovered)
        QTest.mouseMove(widget, merged["rect"].center().toPoint())
        APP.processEvents()
        self.assertGreater(hovered.count(), 0)
        self.assertEqual({item["id"] for item in hovered.at(0)[1]}, {"work1", "work2"})
        count = hovered.count()
        QTest.mouseMove(widget, merged["rect"].center().toPoint() + QPoint(1, 0))
        self.assertEqual(hovered.count(), count)
        widget.close()
        widget.deleteLater()

    def test_sleep_estimate_has_rest_tags_at_both_report_window_boundaries(self):
        widget = TimelineWidget()
        item = {"id": "sleep", "name": "睡觉休息", "category": "休息", "report_ms": LEFT + 3600000,
                "start_ms": None, "end_ms": None, "estimated_intervals": [
                    {"kind": "sleep_report_window", "start_ms": LEFT, "end_ms": LEFT + 3600000}]}
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                             "display_end_ms": LEFT + 120 * 60000, "activities": [item]})
        widget.grab()
        self.assertEqual([tag["category"] for tag in widget.tags], ["休息", "休息"])
        self.assertEqual([tag["y"] for tag in widget.tags], [widget.scale.y(LEFT), widget.scale.y(LEFT + 3600000)])
        self.assertEqual({a["kind"] for a in widget.events()}, {"report", "estimated_start", "estimated_end"})
        self.assertIsNone(item["start_ms"])
        self.assertIsNone(item["end_ms"])
        widget.deleteLater()

    def test_linear_ratio_and_zoom(self):
        origin = LEFT + 9 * 3600000
        scale = TimeScale(origin, origin + 3600000, 2, 0)
        self.assertEqual(scale.y(origin + 5 * 60000), 10)
        self.assertEqual(scale.y(origin + 60 * 60000), 120)
        self.assertEqual(scale.height(origin, origin + 5 * 60000), 10)
        self.assertEqual(scale.height(origin, origin + 60 * 60000), 120)
        zoom = TimeScale(origin, origin + 3600000, 4, 0)
        self.assertEqual(zoom.y(origin + 5 * 60000), 20)
        self.assertEqual(zoom.y(origin + 60 * 60000), 240)

    def test_dense_same_timestamp_keeps_identity_and_exact_anchor(self):
        widget = TimelineWidget()
        items = [{"id": str(i), "name": f"event {i}", "start_ms": LEFT + 60000, "human_intervals": []} for i in range(5)]
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000, "activities": items})
        self.assertEqual(len(widget.anchors), 5)
        self.assertEqual(len(widget.groups), 1)
        before = [a["timestamp"] for a in widget.anchors]
        self.assertEqual(len({a["y"] for a in widget.anchors}), 1)
        widget.set_zoom(4)
        self.assertEqual(before, [a["timestamp"] for a in widget.anchors])
        self.assertEqual({a["item"]["id"] for a in widget.groups[0]}, {str(i) for i in range(5)})
        widget.deleteLater()

    def test_unknown_scalar_has_no_bar_and_report_is_only_report(self):
        widget = TimelineWidget()
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000, "activities": [
            {"id": "scalar", "name": "estimated", "reported_duration_seconds": 1200, "estimated": True},
            {"id": "report", "name": "reported", "report_ms": LEFT + 60000},
            {"id": "actual", "name": "backfill", "start_ms": LEFT + 120000, "report_ms": LEFT + 3600000},
        ]})
        self.assertFalse(widget.bars)
        self.assertEqual([(a["item"]["id"], a["kind"], a["timestamp"]) for a in widget.anchors],
                         [("report", "report", LEFT + 60000), ("actual", "activity", LEFT + 120000)])
        widget.deleteLater()

    def test_ai_separate_same_scale_and_short_height_not_minimized(self):
        widget = TimelineWidget()
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000, "activities": [
            {"id": "a", "start_ms": LEFT, "human_intervals": [[LEFT, LEFT + 6000]], "ai_intervals": [[LEFT, LEFT + 3600000]]}
        ]})
        human, ai = widget.bars
        self.assertAlmostEqual(human["height"], .1 * widget.scale.pixels_per_minute)
        self.assertEqual(ai["height"], 60 * widget.scale.pixels_per_minute)
        self.assertEqual(human["y"], ai["y"])
        widget.deleteLater()

    def test_cross_day_geometry_clips_only_visible_range(self):
        widget = TimelineWidget()
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000, "activities": [
            {"id": "cross", "start_ms": LEFT - 1800000, "end_ms": LEFT + 1800000,
             "recorded_intervals": [[LEFT, LEFT + 1800000]], "human_intervals": [[LEFT, LEFT + 1800000]]}
        ]})
        self.assertEqual(widget.anchors[0]["timestamp"], LEFT)
        self.assertEqual(widget.bars[0]["height"], 30 * widget.scale.pixels_per_minute)
        widget.deleteLater()

    def test_today_axis_stops_at_reliable_backend_now(self):
        widget = TimelineWidget()
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000, "display_end_ms": LEFT + 3600000,
                             "activities": [{"id": "future", "start_ms": LEFT + 7200000}]})
        self.assertEqual(widget.scale.end_ms, LEFT + 3600000)
        self.assertEqual(widget.anchors, [])
        widget.deleteLater()

    def test_only_explicit_end_gets_end_anchor_not_report_or_segment(self):
        widget = TimelineWidget()
        widget.set_snapshot({"day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                             "activities": [{"id": "end", "end_ms": LEFT + 60000, "report_ms": LEFT + 3600000}]})
        self.assertEqual([(a["kind"], a["timestamp"]) for a in widget.anchors], [("end", LEFT + 60000)])
        self.assertEqual(widget.bars, [])
        widget.deleteLater()


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(self.temp.name + "/ui.ini", QSettings.Format.IniFormat)
        self.controller = FakeController()
        self.window = DesktopWindow(self.controller, settings=self.settings, create_tray=False)

    def tearDown(self):
        self.window._closing = True
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def _scrollable_history(self, chosen="2026-10-26"):
        self.window.date_edit.setDate(QDate.fromString(chosen, "yyyy-MM-dd"))
        left = int(datetime.fromisoformat(chosen).replace(tzinfo=TZ).timestamp() * 1000)
        self.window.receive_pending([{"text": f"pending record {n}", "report_ms": left + n * 60000,
                                     "stage": "server_saved"} for n in range(8)])
        return {"date": chosen, "day_start_ms": left, "day_end_ms": left + 86400000,
                "display_end_ms": left + 10 * 3600000, "activities": [
                    {"id": "start", "name": "morning", "start_ms": left + 3600000},
                    {"id": "latest", "name": "latest", "start_ms": left + 9 * 3600000}],
                "summary": {}}

    def _settle_history_layout(self):
        # Scroll range changes and the overview fit each post another Qt event.
        for _ in range(5):
            APP.processEvents()
            QTest.qWait(1)

    def test_default_latest_waits_for_layout_on_initial_date_switch_reopen_and_expand(self):
        value = self._scrollable_history()
        pending = list(self.window._pending)
        self.window.receive_pending([])
        self.window.set_expanded(True)
        self.window.show()
        self.window.receive_snapshot(value)
        # The controller may deliver pending items after the snapshot signal.
        self.window.receive_pending(pending)
        self._settle_history_layout()
        scroll = self.window.scroll.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 0)
        self.assertEqual(scroll.value(), scroll.maximum())

        scroll.setValue(0)
        self.window.hide_to_tray()
        with patch("time_logger.desktop_ui.ensure_native_visible"):
            self.window.show_and_focus()
        self._settle_history_layout()
        self.assertEqual(scroll.value(), scroll.maximum())

        scroll.setValue(0)
        self.window.set_expanded(False)
        self.window.set_expanded(True)
        self._settle_history_layout()
        self.assertEqual(scroll.value(), scroll.maximum())

        scroll.setValue(0)
        value = self._scrollable_history("2026-10-25")
        self.window.receive_snapshot(value)
        self._settle_history_layout()
        self.assertEqual(scroll.value(), scroll.maximum())

    def test_background_snapshot_keeps_manual_history_position(self):
        value = self._scrollable_history()
        self.window.set_expanded(True)
        self.window.show()
        self.window.receive_snapshot(value)
        self._settle_history_layout()
        scroll = self.window.scroll.verticalScrollBar()
        scroll.setValue(scroll.maximum() // 2)
        before = scroll.value()
        self.assertGreater(before, 0)
        self.window.receive_snapshot(value)
        self._settle_history_layout()
        self.assertEqual(scroll.value(), before)

    def test_example_hides_on_focus_and_preedit_and_returns_only_when_idle_empty(self):
        edit = self.window.editor
        example = edit.placeholderText()
        self.assertTrue(example)
        self.window.show()
        self.window.activateWindow()
        self.window.expand_button.setFocus()
        APP.processEvents()
        self.assertFalse(edit.hasFocus())
        self.assertEqual(edit.placeholderText(), example)

        QTest.mouseClick(edit.viewport(), Qt.MouseButton.LeftButton)
        self.assertTrue(edit.hasFocus())
        self.assertEqual(edit.placeholderText(), "")
        edit.inputMethodEvent(QInputMethodEvent("youzimu", []))
        self.assertEqual(edit.toPlainText(), "")
        self.assertEqual(edit.placeholderText(), "")
        edit.inputMethodEvent(QInputMethodEvent("", []))
        self.assertEqual(edit.placeholderText(), "")

        edit.clearFocus()
        self.assertEqual(edit.placeholderText(), example)
        # Preedit is not committed document text; it must hide the example too.
        edit.inputMethodEvent(QInputMethodEvent("youzimu", []))
        self.assertEqual(edit.placeholderText(), "")
        commit = QInputMethodEvent()
        commit.setCommitString("有字母")
        edit.inputMethodEvent(commit)
        self.assertEqual(edit.toPlainText(), "有字母")
        self.assertEqual(edit.placeholderText(), "")
        edit.clear()
        self.assertEqual(edit.placeholderText(), example)
        self.assertFalse(self.controller.calls)

    def test_enter_records_ctrl_enter_newline_and_ime_commit_no_send(self):
        edit = self.window.editor
        edit.setPlainText("one")
        QTest.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertIn("\n", edit.toPlainText())
        self.assertFalse(self.controller.calls)
        QTest.keyClick(edit, Qt.Key.Key_Return)
        self.assertEqual(len(self.controller.calls), 1)
        edit.inputMethodEvent(QInputMethodEvent("zhong", []))
        QTest.keyClick(edit, Qt.Key.Key_Return)
        self.assertEqual(len(self.controller.calls), 1)
        event = QInputMethodEvent()
        event.setCommitString("中")
        edit.inputMethodEvent(event)
        QTest.keyClick(edit, Qt.Key.Key_Return)
        self.assertEqual(len(self.controller.calls), 1)
        QTest.keyClick(edit, Qt.Key.Key_Return)
        self.assertEqual(len(self.controller.calls), 2)
        self.assertEqual(self.controller.calls[-1][0], "中")
        self.assertEqual(edit.toPlainText(), "")

    def test_overview_fits_long_range_uniformly_and_resize_preserves_zoom_choice(self):
        self.window.date_edit.setDate(QDate(2026, 10, 26))
        self.window.set_expanded(True)
        self.window.show()
        APP.processEvents()
        value = {"date": "2026-10-26", "day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                 "display_end_ms": LEFT + 10 * 3600000, "summary": {}, "activities": [
                     {"id": "sleep", "name": "sleep", "category": "休息", "start_ms": LEFT + 3600000,
                      "end_ms": LEFT + 6 * 3600000},
                     {"id": "short", "category": "工作", "start_ms": LEFT + 9 * 3600000,
                      "end_ms": LEFT + 9 * 3600000 + 5 * 60000}]}
        self.window.receive_snapshot(value)
        APP.processEvents()
        APP.processEvents()
        scale = self.window.timeline.scale
        self.assertLess(scale.pixels_per_minute, 3)
        self.assertLessEqual(self.window.timeline.height(), self.window.scroll.viewport().height())
        self.assertAlmostEqual(scale.height(LEFT, LEFT + 300 * 60000) /
                               scale.height(LEFT, LEFT + 5 * 60000), 60)
        self.window.set_time_zoom(8)
        self.window.resize(540, 900)
        APP.processEvents()
        self.window.receive_snapshot(value)
        APP.processEvents()
        self.assertEqual(self.window.timeline.scale.pixels_per_minute, 8)
        self.assertTrue(self.window.zoom_button.isChecked())
        self.window.set_time_zoom(3)
        APP.processEvents()
        self.assertLess(self.window.timeline.scale.pixels_per_minute, 3)
        self.assertTrue(self.window.overview_button.isChecked())
        self.assertEqual(self.window.editor.height(), 74)
        self.assertFalse(self.controller.calls)

    def test_hover_is_dismissed_on_date_change_and_window_hide(self):
        self.window.show()
        APP.processEvents()
        card = self.window.timeline_hover
        from PySide6.QtCore import QPoint, QRect
        card.show_records(QPoint(200, 200), [{"id": "x", "category": "学习", "name": "record"}],
                          QRect(195, 195, 50, 20))
        self.assertTrue(card.isVisible())
        self.window.date_edit.setDate(QDate(2020, 1, 1))
        self.assertFalse(card.isVisible())
        card.show_records(QPoint(200, 200), [{"id": "x", "category": "学习", "name": "record"}],
                          QRect(195, 195, 50, 20))
        self.assertTrue(card.isVisible())
        self.window.hide()
        self.assertFalse(card.isVisible())

    def test_failed_local_save_never_clears_draft_or_claims_saved(self):
        self.controller.fail = True
        self.window.editor.setPlainText("keep me")
        self.window.submit()
        self.assertEqual(self.window.editor.toPlainText(), "keep me")
        self.assertIn("尚未存下", self.window.save_state.text())
        self.assertFalse(self.controller.calls)

    def test_retry_recovers_existing_queue_without_submitting_or_clearing_draft(self):
        self.window.editor.setPlainText("keep this unsent draft")
        self.window.show()
        APP.processEvents()
        QTest.mouseClick(self.window.retry_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.controller.retry_calls, 1)
        self.assertFalse(self.controller.calls)
        self.assertEqual(self.window.editor.toPlainText(), "keep this unsent draft")
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertFalse(self.window.retry_action.isEnabled())
        self.assertIn("同步中", self.window.retry_button.text())
        self.assertTrue(self.window.record_button.isEnabled())
        self.window._retry_sync()
        self.assertEqual(self.controller.retry_calls, 1)
        self.controller.state.emit("同步暂时受阻，原文已保留")
        self.controller.sync_busy.emit(False)
        self.assertTrue(self.window.retry_button.isEnabled())
        self.assertTrue(self.window.retry_action.isEnabled())
        self.assertEqual(self.window.retry_button.text(), "立即重试")
        self.window.retry_action.trigger()
        self.assertEqual(self.controller.retry_calls, 2)
        self.assertFalse(self.controller.calls)

    def test_retry_error_leaves_draft_and_retry_available(self):
        self.controller.retry_fail = True
        self.window.editor.setPlainText("keep unsent")
        self.window._retry_sync()
        self.assertTrue(self.window.retry_button.isEnabled())
        self.assertTrue(self.window.retry_action.isEnabled())
        self.assertEqual(self.window.editor.toPlainText(), "keep unsent")
        self.assertFalse(self.controller.calls)
        self.assertIn("原文", self.window.save_state.text())

    def test_retry_stays_accessible_in_narrow_compact_and_expanded_window(self):
        self.window.show()
        self.window._receive_state("同步暂时受阻，原文已保留；联网后会自动继续，也可立即重试。" * 2)
        for expanded in (False, True):
            self.window.set_expanded(expanded)
            self.window.resize(460, 740 if expanded else 300)
            APP.processEvents()
            self.assertEqual(self.window.width(), 460)
            self.assertEqual(self.window.editor.height(), 74)
            self.assertTrue(self.window.retry_button.isVisible())
            self.assertTrue(self.window.record_button.isVisible())
            retry = self.window.retry_button.mapTo(self.window, self.window.retry_button.rect().bottomRight())
            record = self.window.record_button.mapTo(self.window, self.window.record_button.rect().bottomRight())
            self.assertTrue(self.window.rect().contains(retry))
            self.assertTrue(self.window.rect().contains(record))

    def test_failed_draft_flush_keeps_window_visible_for_update_or_close(self):
        self.window.show()
        self.window.editor.setPlainText("keep unsaved")
        self.controller.fail = True
        APP.processEvents()
        self.assertFalse(self.window.hide_to_tray())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.editor.toPlainText(), "keep unsaved")

    def test_restored_draft_never_submits(self):
        self.controller.draft = "unsent"
        restored = DesktopWindow(self.controller, settings=self.settings, create_tray=False)
        self.assertEqual(restored.editor.toPlainText(), "unsent")
        self.assertFalse(self.controller.calls)
        restored._closing = True
        restored.close()
        restored.deleteLater()

    def test_old_date_does_not_change_submission_payload(self):
        self.window.date_edit.setDate(QDate(2020, 1, 1))
        self.window.editor.setPlainText("now")
        self.window.submit()
        self.assertEqual(self.controller.calls, [("now", {})])

    def test_date_change_clears_old_nodes_while_loading(self):
        self.window.date_edit.setDate(QDate(2026, 10, 26))
        self.window.receive_snapshot({"date": "2026-10-26", "day_start_ms": LEFT,
                                      "day_end_ms": LEFT + 86400000,
                                      "activities": [{"id": "old", "name": "old day", "start_ms": LEFT + 60000}],
                                      "summary": {}})
        self.assertEqual(self.window.node_select.count(), 1)
        self.window.date_edit.setDate(QDate(2026, 10, 25))
        self.assertEqual(self.window.node_select.count(), 0)
        self.assertFalse(self.window.timeline.events())
        self.assertIsNone(self.window.timeline.selected_key)
        self.assertIn("正在读取", self.window.detail_label.text())

    def test_scroll_and_compact_keep_composer_outside_history(self):
        self.window.set_expanded(True)
        self.window.show()
        APP.processEvents()
        self.assertIsNot(self.window.editor.parent(), self.window.scroll.widget())
        before = self.window.composer.mapTo(self.window, self.window.composer.rect().topLeft())
        self.window.scroll.verticalScrollBar().setValue(1000)
        APP.processEvents()
        self.assertEqual(before, self.window.composer.mapTo(self.window, self.window.composer.rect().topLeft()))
        self.window.set_expanded(False)
        APP.processEvents()
        self.assertTrue(self.window.editor.isVisible())
        self.assertFalse(self.window.history.isVisible())

    def test_null_human_is_unknown_and_summary_same_snapshot(self):
        self.window.date_edit.setDate(QDate(2026, 10, 26))
        self.window.receive_snapshot({"date": "2026-10-26", "day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                                      "as_of_ms": LEFT + 80000000, "activities": [], "summary": {"human_seconds": None, "ai_seconds": None}})
        self.assertIn("未知", self.window.summary_label.text())
        self.assertNotIn("0秒", self.window.summary_label.text())
        self.assertEqual(self.window._snapshot, self.window.timeline.snapshot)

    def test_estimated_float_scalar_has_only_one_approximation_prefix(self):
        item = {"id": "estimated-scalar", "name": "estimated duration", "estimated": True,
                "reported_duration_seconds": 65.5}
        self.window.date_edit.setDate(QDate(2026, 10, 26))
        self.window.receive_snapshot({"date": "2026-10-26", "day_start_ms": LEFT,
                                      "day_end_ms": LEFT + 86400000,
                                      "activities": [item], "summary": {"scalar_seconds": 65.5}})
        label = self.window.unlocated.item(0).text()
        self.assertIn("约1分钟", label)
        self.assertNotIn("约约", label)
        self.assertNotIn("约约", self.window.summary_label.text())
        dialog = ActivityDialog([item], self.window)
        duration_row = next(dialog.details.topLevelItem(n) for n in range(dialog.details.topLevelItemCount())
                            if dialog.details.topLevelItem(n).text(0) == "单独报告的时长")
        self.assertIn("约1分钟", duration_row.text(1))
        self.assertNotIn("约约", duration_row.text(1))
        dialog.deleteLater()

    def test_question_answer_and_skip_share_stable_id(self):
        self.window.receive_questions([{"question_id": "stable", "text": "何时结束？"}])
        self.window._answer_question()
        self.window.editor.setPlainText("15:00")
        self.window.submit()
        self.assertEqual(self.controller.calls[0], ("15:00", {"question_id": "stable", "answer_action": "answer"}))
        self.window._skip_question()
        self.assertEqual(self.controller.calls[1], ("", {"question_id": "stable", "answer_action": "skip"}))

    def test_close_hides_without_stopping_controller(self):
        self.window.show()
        APP.processEvents()
        self.window.close()
        APP.processEvents()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.controller.visible)

    def test_pending_stage_and_stale_cache_are_honest(self):
        self.window.receive_pending([{"text": "offline", "reported_at_ms": LEFT, "stage": "local_saved", "error": "network"}])
        self.assertIn("已存本机，待上传", self.window.pending_list.item(0).text())
        self.assertNotIn("服务器已保存", self.window.pending_list.item(0).text())
        self.window.date_edit.setDate(QDate(2026, 10, 26))
        self.window.receive_snapshot({"date": "2026-10-26", "day_start_ms": LEFT, "day_end_ms": LEFT + 86400000,
                                      "as_of_ms": LEFT + 80000000, "activities": [], "summary": {}, "cache": {"local_cached": True, "stale": True}})
        self.assertTrue(self.window.sync_label.text().startswith("缓存截至"))

    def test_chat_mirror_receipt_keeps_original_stage_in_pending(self):
        self.window.receive_pending([{"text": "saved", "reported_at_ms": LEFT, "stage": "server_saved", "chat_mirror_status": "已同步到飞书聊天"}])
        label = self.window.pending_list.item(0).text()
        self.assertIn("服务器已保存，待整理", label)
        self.assertIn("已同步到飞书聊天", label)
        self.assertNotIn("已归档", label)

    def test_legacy_correction_uses_record_fingerprint_not_pseudo_activity(self):
        self.window._correct_activity({"id": "base:stable", "record_id": "stable", "record_fingerprint": "fingerprint", "name": "old"})
        self.window.editor.setPlainText("更正")
        self.window.submit()
        self.assertEqual(self.controller.calls[0], ("更正", {"record_id": "stable", "expected_fingerprint": "fingerprint"}))


if __name__ == "__main__":
    unittest.main()
