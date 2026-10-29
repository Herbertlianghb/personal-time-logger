"""Floating-window behavior with synthetic input and temporary settings only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QSettings, QEvent, Qt, Signal
from PySide6.QtGui import QInputMethodEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QWidget

from time_logger.desktop_floating import FloatingPanel
from time_logger.desktop_transition import WindowTransition
from time_logger.desktop_ui import DesktopWindow


APP = QApplication.instance() or QApplication([])


class FakeController(QObject):
    snapshot = Signal(dict)
    state = Signal(str)
    pending = Signal(list)
    questions = Signal(list)
    sync_busy = Signal(bool)
    heatmap_snapshot = Signal(dict)
    heatmap_state = Signal(str)

    def __init__(self):
        super().__init__()
        self.draft = ""
        self.fail = False
        self.visible = None
        self.heatmap_visible = False

    def request_heatmap(self, month, *, refresh=False, scope="year"):
        self.heatmap_visible = True

    def set_heatmap_visible(self, visible):
        self.heatmap_visible = visible

    def get_draft(self):
        return self.draft

    def set_draft(self, value):
        if self.fail:
            raise OSError("read_only")
        self.draft = value

    def refresh(self, day):
        pass

    def load_cached(self, day):
        pass

    def set_visible(self, visible):
        self.visible = visible


class FloatingPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.controller = FakeController()
        self.window = DesktopWindow(self.controller, settings=self.settings, create_tray=False)
        # The offscreen backend cannot represent another foreground Windows app.
        # Keep its focus state explicit; these tests never move the real cursor.
        self.pointer = QPoint(-100000, -100000)
        self.cursor_patch = patch("time_logger.desktop_floating.QCursor.pos", side_effect=lambda: self.pointer)
        self.active_patch = patch.object(self.window, "isActiveWindow", return_value=False)
        self.cursor_patch.start()
        self.active = self.active_patch.start()
        self.panel = FloatingPanel(self.window)
        self.window.floating_panel = self.panel
        self.window._apply_topmost()
        self.panel.start()
        APP.processEvents()

    def tearDown(self):
        self.panel.dispose()
        self.window._closing = True
        self.window.close()
        self.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.active_patch.stop()
        self.cursor_patch.stop()
        self.temp.cleanup()

    def hover(self):
        self.pointer = self.panel.entry.frameGeometry().center()
        self.panel.entry.entered.emit()
        QTest.qWait(FloatingPanel.HOVER_DELAY_MS + WindowTransition.DURATION_MS + 100)
        self.assertTrue(self.window.isVisible())

    def leave(self):
        self.pointer = QPoint(-100000, -100000)
        self.panel.entry.left.emit()
        # Includes one watch interval and the retract-to-entry animation.
        QTest.qWait(FloatingPanel.LEAVE_DELAY_MS + WindowTransition.DURATION_MS + 250)

    def test_hover_is_delayed_and_does_not_request_focus(self):
        with patch.object(self.window, "activateWindow") as activate, \
             patch.object(self.window.editor, "setFocus") as focus, \
             patch("time_logger.desktop_ui.ensure_native_visible") as native_show:
            self.pointer = self.panel.entry.frameGeometry().center()
            self.panel.entry.entered.emit()
            QTest.qWait(50)
            self.assertFalse(self.window.isVisible())
            QTest.qWait(FloatingPanel.HOVER_DELAY_MS + WindowTransition.DURATION_MS + 100)
            self.assertTrue(self.window.isVisible())
            self.assertTrue(self.window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
            activate.assert_not_called()
            focus.assert_not_called()
            self.assertFalse(native_show.call_args.kwargs["activate"])

    def test_passing_over_entry_does_not_expand(self):
        self.pointer = self.panel.entry.frameGeometry().center()
        self.panel.entry.entered.emit()
        QTest.qWait(30)
        self.pointer = QPoint(-100000, -100000)
        self.panel.entry.left.emit()
        QTest.qWait(FloatingPanel.HOVER_DELAY_MS + 50)
        self.assertFalse(self.window.isVisible())

    def test_click_uses_explicit_focus_path(self):
        with patch.object(self.window, "show_and_focus", wraps=self.window.show_and_focus) as focus_show:
            QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
            focus_show.assert_called_once()
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))

    def test_leaving_collapses_through_draft_save_and_keeps_entry(self):
        self.hover()
        with patch.object(self.controller, "set_draft", wraps=self.controller.set_draft) as save:
            self.leave()
            save.assert_called_once_with("")
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.controller.visible)
        self.assertTrue(self.panel.entry.isVisible())

    def test_click_toggles_and_saves_draft_even_when_kept_open(self):
        self.hover()
        self.window.editor.setPlainText("点击收起保留草稿")
        self.panel.set_keep_open(True)
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.controller.draft, "点击收起保留草稿")
        self.panel.entry.entered.emit()
        QTest.qWait(FloatingPanel.HOVER_DELAY_MS + WindowTransition.DURATION_MS + 100)
        self.assertFalse(self.window.isVisible())
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.editor.toPlainText(), self.controller.draft)
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.panel.entry.left.emit()
        self.hover()

    def test_click_during_reveal_cancels_and_during_conceal_reopens(self):
        self.pointer = self.panel.entry.frameGeometry().center()
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        self.assertTrue(self.panel.transition.revealing)
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertFalse(self.window.isVisible())
        self.panel.entry.left.emit()
        self.hover()
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        self.assertTrue(self.panel.transition.running)
        self.assertFalse(self.panel.transition.revealing)
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertTrue(self.window.isVisible())

    def test_click_keeps_ime_and_failed_draft_save_protected(self):
        self.hover()
        APP.sendEvent(self.window.editor, QInputMethodEvent("zhong", []))
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window.isVisible())
        APP.sendEvent(self.window.editor, QInputMethodEvent("", []))
        self.controller.fail = True
        with patch.object(self.controller, "set_draft", wraps=self.controller.set_draft) as save:
            QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
            self.assertTrue(self.window.isVisible())
            save.assert_called_once()
        self.assertFalse(self.panel._watch.isActive())

    def test_moving_from_entry_into_panel_keeps_it_open(self):
        self.hover()
        self.panel.entry.left.emit()
        self.pointer = self.window.frameGeometry().center()
        QTest.qWait(FloatingPanel.LEAVE_DELAY_MS + 250)
        self.assertTrue(self.window.isVisible())

    def test_unsent_draft_is_saved_when_mouse_leaves_and_restored_on_reveal(self):
        self.hover()
        self.window.editor.setPlainText("尚未提交的内容")
        self.leave()
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.window.editor.toPlainText(), "尚未提交的内容")
        self.assertEqual(self.controller.draft, "尚未提交的内容")
        self.hover()
        self.assertEqual(self.window.editor.toPlainText(), "尚未提交的内容")

    def test_ime_preedit_blocks_collapse_but_focus_alone_does_not(self):
        self.hover()
        APP.sendEvent(self.window.editor, QInputMethodEvent("zhong", []))
        self.assertTrue(self.window.editor.composing)
        self.leave()
        self.assertTrue(self.window.isVisible())
        APP.sendEvent(self.window.editor, QInputMethodEvent("", []))
        self.active.return_value = True
        with patch.object(self.window.editor, "hasFocus", return_value=True):
            self.leave()
        self.assertFalse(self.window.isVisible())

    def test_floating_panel_is_frameless_tool_and_normal_mode_restores_window(self):
        self.assertEqual(self.window.windowType(), Qt.WindowType.Tool)
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.window._floating_changed(False)
        self.assertEqual(self.window.windowType(), Qt.WindowType.Window)
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)

    def test_keep_open_can_be_released_without_losing_auto_collapse(self):
        self.hover()
        self.panel.set_keep_open(True)
        self.leave()
        self.assertTrue(self.window.isVisible())
        self.panel.set_keep_open(False)
        self.leave()
        self.assertFalse(self.window.isVisible())

    def test_owned_nonmodal_dialog_and_popup_prevent_collapse(self):
        self.hover()
        dialog = QDialog(self.window)
        dialog.show()
        self.leave()
        self.assertTrue(self.window.isVisible())
        dialog.hide()
        menu = QMenu(self.window)
        with patch("time_logger.desktop_floating.QApplication.activePopupWidget", return_value=menu):
            self.leave()
        self.assertTrue(self.window.isVisible())
        self.leave()
        self.assertFalse(self.window.isVisible())

    def test_heatmap_pointer_keeps_group_open_then_leave_retracts_both(self):
        self.hover()
        self.window.editor.setPlainText("热力图收起保留草稿")
        self.window.show_heatmap()
        dialog = self.window._heatmap_dialog
        dialog.set_mode("weekly")
        self.pointer = dialog.frameGeometry().center()
        APP.processEvents()
        self.panel._check_pointer()
        self.panel._collapse()
        self.assertTrue(dialog.isVisible())
        self.assertFalse(self.panel._leave.isActive())
        self.pointer = QPoint(-100000, -100000)
        self.panel._check_pointer()
        self.assertTrue(self.panel._leave.isActive())
        QTest.qWait(FloatingPanel.LEAVE_DELAY_MS + 40)
        self.assertTrue(self.panel.heatmap_transition.running)
        self.assertEqual(self.panel.heatmap_transition.animation.endValue(), self.panel.entry.geometry())
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.controller.heatmap_visible)
        self.assertEqual(self.controller.draft, "热力图收起保留草稿")
        self.window.show_heatmap()
        self.assertIs(self.window._heatmap_dialog, dialog)
        self.assertEqual(dialog.mode, "weekly")

    def test_heatmap_alone_is_watched_and_entry_click_closes_it(self):
        self.window.show_heatmap()
        dialog = self.window._heatmap_dialog
        self.pointer = dialog.frameGeometry().center()
        APP.processEvents()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.panel._watch.isActive())
        QTest.mouseClick(self.panel.entry, Qt.MouseButton.LeftButton)
        QTest.qWait(WindowTransition.DURATION_MS + 100)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(self.controller.heatmap_visible)
        self.assertTrue(self.panel.entry.isVisible())

    def test_heatmap_respects_keep_open_popup_save_failure_and_normal_mode(self):
        self.hover()
        self.window.show_heatmap()
        dialog = self.window._heatmap_dialog
        self.pointer = QPoint(-100000, -100000)
        self.panel.set_keep_open(True)
        self.panel._collapse()
        self.assertTrue(dialog.isVisible())
        self.panel.set_keep_open(False)
        with patch("time_logger.desktop_floating.QApplication.activePopupWidget", return_value=dialog.scope_combo):
            self.panel._collapse()
            self.assertTrue(dialog.isVisible())
        self.controller.fail = True
        self.panel._collapse()
        self.assertTrue(dialog.isVisible())
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.panel._watch.isActive())
        self.controller.fail = False
        self.panel.set_enabled(False)
        self.panel._collapse()
        self.assertTrue(dialog.isVisible())

    def test_failed_draft_save_keeps_panel_open_without_retry_loop(self):
        self.hover()
        self.controller.fail = True
        with patch.object(self.controller, "set_draft", wraps=self.controller.set_draft) as save:
            self.leave()
            self.assertTrue(self.window.isVisible())
            QTest.qWait(FloatingPanel.LEAVE_DELAY_MS + 250)
            save.assert_called_once_with("")

    def test_disabling_cancels_pending_hover_and_persists_preference(self):
        self.pointer = self.panel.entry.frameGeometry().center()
        self.panel.entry.entered.emit()
        self.panel.set_enabled(False)
        self.window.hide_to_tray()
        QTest.qWait(FloatingPanel.HOVER_DELAY_MS + 100)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.panel.entry.isVisible())
        self.assertFalse(self.settings.value("floating/enabled", True, type=bool))

    def test_dispose_cancels_queued_actions_without_resurrecting_panel(self):
        self.pointer = self.panel.entry.frameGeometry().center()
        self.panel.entry.entered.emit()
        self.panel.dispose()
        self.panel.set_enabled(False)
        self.panel.start()
        # Emit before Qt deletes the entry; queued quit-time clicks are harmless.
        self.panel.entry.clicked.emit()
        QTest.qWait(FloatingPanel.HOVER_DELAY_MS + 100)
        self.assertFalse(self.window.isVisible())

    def test_dispose_ignores_late_resize_callback_after_entry_deletion(self):
        self.hover()
        self.panel.dispose()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        # A singleShot queued by Resize may arrive after deleteLater completes.
        # Invoke that callback directly so any use-after-delete fails the test.
        self.panel._position_panel()
        self.assertTrue(self.window.isVisible())

    def test_drag_snaps_to_edge_and_saves_position_without_click(self):
        entry = self.panel.entry
        area = APP.primaryScreen().availableGeometry()
        local = QPointF(10, 10)
        start = QPointF(entry.mapToGlobal(QPoint(10, 10)))
        destination = QPointF(area.left() + 12, area.top() + 100)
        event_specs = (
            (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseMove, destination, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, destination, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
        )
        with patch.object(self.window, "show_and_focus") as activate:
            # Construct and deliver in real input order, without retaining future
            # pointer events for the same device before the press is processed.
            for kind, position, button, buttons in event_specs:
                APP.sendEvent(entry, QMouseEvent(kind, local, position, button, buttons,
                                                Qt.KeyboardModifier.NoModifier))
            activate.assert_not_called()
        self.assertEqual(entry.x(), area.left())
        self.assertTrue(area.contains(entry.frameGeometry()))
        self.assertEqual(self.settings.value("floating/position"), entry.pos())


class WindowTransitionTests(unittest.TestCase):
    def setUp(self):
        self.window = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.window.setGeometry(QRect(100, 100, 460, 300))
        self.anchor = QRect(700, 220, 32, 64)
        self.transition = WindowTransition(self.window)

    def tearDown(self):
        self.transition.dispose()
        self.window.close()
        self.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()

    def finish(self):
        QTest.qWait(WindowTransition.DURATION_MS + 100)

    def test_reveal_travels_from_entry_to_panel_without_resizing_real_window(self):
        geometry = self.window.geometry()
        show = Mock(side_effect=self.window.show)
        self.transition.reveal(self.window, self.anchor, show)
        self.assertEqual(self.transition.animation.startValue(), self.anchor)
        self.assertEqual(self.transition.animation.endValue(), geometry)
        self.assertEqual(self.transition.overlay.geometry(), self.anchor)
        self.assertTrue(self.transition.overlay.isVisible())
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.window.geometry(), geometry)
        self.finish()
        show.assert_called_once_with()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.geometry(), geometry)
        self.assertFalse(self.transition.overlay.isVisible())

    def test_conceal_ends_at_entry_and_keeps_real_panel_geometry(self):
        self.window.show()
        APP.processEvents()
        geometry = self.window.geometry()
        hide = Mock(side_effect=self.window.hide)
        self.transition.conceal(self.window, self.anchor, hide)
        hide.assert_called_once_with()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.transition.overlay.isVisible())
        self.assertEqual(self.transition.animation.startValue(), geometry)
        self.assertEqual(self.transition.animation.endValue(), self.anchor)
        self.assertEqual(self.window.geometry(), geometry)
        self.assertTrue(self.transition.overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(self.transition.overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.finish()
        self.assertEqual(self.transition.overlay.geometry(), self.anchor)
        self.assertFalse(self.transition.overlay.isVisible())
        self.assertEqual(self.window.geometry(), geometry)

    def test_cancelled_reveal_does_not_show_later_and_can_be_restarted(self):
        show = Mock(side_effect=self.window.show)
        self.transition.reveal(self.window, self.anchor, show)
        self.transition.cancel()
        self.finish()
        show.assert_not_called()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.transition.overlay.isVisible())
        self.transition.reveal(self.window, self.anchor, show)
        self.finish()
        show.assert_called_once_with()
        self.assertTrue(self.window.isVisible())

    def test_reopening_during_conceal_finishes_visible_at_original_size(self):
        self.window.show()
        APP.processEvents()
        geometry = self.window.geometry()
        self.transition.conceal(self.window, self.anchor, self.window.hide)
        self.transition.reveal(self.window, self.anchor, self.window.show)
        self.finish()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.geometry(), geometry)
        self.assertFalse(self.transition.overlay.isVisible())

    def test_dispose_drops_reveal_callback_and_ignores_late_requests(self):
        show = Mock(side_effect=self.window.show)
        hide = Mock(side_effect=self.window.hide)
        self.transition.reveal(self.window, self.anchor, show)
        self.transition.dispose()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.transition.reveal(self.window, self.anchor, show)
        self.transition.conceal(self.window, self.anchor, hide)
        self.transition.animation.finished.emit()
        self.finish()
        show.assert_not_called()
        hide.assert_not_called()
        self.assertFalse(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
