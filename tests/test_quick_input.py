"""Independent pinned input shares the existing draft and submission pipeline."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from tests.test_desktop_ui import APP, FakeController
from time_logger.desktop_ui import DesktopWindow


class QuickInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.controller = FakeController()
        self.controller.draft = "原有草稿"
        self.window = DesktopWindow(self.controller, settings=self.settings, create_tray=False)

    def tearDown(self):
        quick = getattr(self.window, "_quick_input", None)
        if quick:
            quick.shutdown()
            quick.deleteLater()
        self.window._closing = True
        self.window.close()
        self.window.deleteLater()
        APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.temp.cleanup()

    def open(self):
        self.window.show_quick_input(focus=False)
        APP.processEvents()
        return self.window._quick_input

    def test_shared_draft_and_independent_visibility(self):
        quick = self.open()
        self.assertEqual(quick.editor.toPlainText(), "原有草稿")
        quick.editor.setPlainText("输入小窗修改后的草稿")
        self.assertEqual(self.window.editor.toPlainText(), "输入小窗修改后的草稿")
        self.window.hide_to_tray()
        APP.processEvents()
        self.assertTrue(quick.isVisible())
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.controller.draft, "输入小窗修改后的草稿")
        self.assertIsNone(quick.parentWidget())

    def test_pin_toggle_keeps_contents_and_remembers_selection(self):
        quick = self.open()
        self.assertTrue(quick.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        quick.pin.setChecked(False)
        self.assertFalse(quick.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(quick.isVisible())
        self.assertEqual(quick.editor.toPlainText(), "原有草稿")
        self.assertFalse(self.settings.value("quick_input/pinned", type=bool))
        quick.pin.setChecked(True)
        self.assertTrue(quick.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def test_submit_uses_one_existing_queue_and_clears_both_editors(self):
        quick = self.open()
        quick.editor.setPlainText("新的真实输入")
        quick.submit()
        self.assertEqual(self.controller.calls, [("新的真实输入", {})])
        self.assertEqual(quick.editor.toPlainText(), "")
        self.assertEqual(self.window.editor.toPlainText(), "")
        self.assertEqual(self.controller.draft, "")
        self.assertTrue(quick.isVisible())

    def test_failed_submit_retains_shared_text(self):
        quick = self.open()
        self.controller.fail = True
        quick.submit()
        self.assertEqual(quick.editor.toPlainText(), "原有草稿")
        self.assertEqual(self.window.editor.toPlainText(), "原有草稿")
        self.assertFalse(self.controller.calls)
        self.assertIn("尚未存下", quick.save_state.text())

    def test_composition_blocks_submission_and_close(self):
        quick = self.open()
        APP.sendEvent(quick.editor, QInputMethodEvent("xue", []))
        quick.submit()
        self.assertFalse(self.controller.calls)
        self.assertFalse(quick.record_button.isEnabled())
        quick.close()
        self.assertTrue(quick.isVisible())
        APP.sendEvent(quick.editor, QInputMethodEvent("", []))

    def test_correction_context_is_visible_and_preserved(self):
        quick = self.open()
        self.window._correct_activity({"record_id": "rec-test", "record_fingerprint": "fingerprint", "name": "所选活动"})
        self.assertIn("所选活动", quick.context_label.text())
        quick.submit()
        self.assertEqual(self.controller.calls[0][1], {"record_id": "rec-test", "expected_fingerprint": "fingerprint"})
        self.assertFalse(quick.context_frame.isVisible())

    def test_close_saves_draft_and_reopen_uses_same_window(self):
        quick = self.open()
        quick.editor.setPlainText("关闭时保留")
        quick.close()
        self.assertEqual(self.controller.draft, "关闭时保留")
        self.assertFalse(self.settings.value("quick_input/open", type=bool))
        self.window.show_quick_input(focus=False)
        self.assertIs(self.window._quick_input, quick)
        self.assertTrue(quick.isVisible())
        self.assertEqual(quick.editor.toPlainText(), "关闭时保留")

    def test_background_status_never_activates_quick_window(self):
        quick = self.open()
        with patch.object(quick, "activateWindow") as activate:
            self.controller.state.emit("服务器已保存原文")
            APP.processEvents()
            self.assertEqual(quick.save_state.text(), "服务器已保存原文")
            activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
