"""Startup checks use temporary paths and mocked IPC, never the live app."""
import errno
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from time_logger import desktop_app

APP = QApplication.instance() or QApplication([])


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.config_path = self.folder / "device.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_data_directory_does_not_change_with_appdata_view(self):
        expected = self.folder / ".personal-time-logger"
        for view in ("ordinary-local", "codex-private-local"):
            with self.subTest(view=view), patch.dict(os.environ, {
                "USERPROFILE": str(self.folder), "LOCALAPPDATA": str(self.folder / view)
            }):
                self.assertEqual(desktop_app.data_directory(), expected)

    def test_startup_uses_one_root_for_config_lock_queue_settings_and_health(self):
        expected = self.folder / ".personal-time-logger"
        expected.mkdir()
        (expected / "device.json").write_text('{"fixture": true}', encoding="utf-8")
        for view in ("ordinary-local", "codex-private-local"):
            lock, server, controller, window, shortcut, app = (Mock() for _ in range(6))
            lock.tryLock.return_value = True
            server.listen.return_value = True
            window.isVisible.return_value = False
            window.winId.return_value = 123
            shortcut.label = "fixture hotkey"
            app.exec.return_value = 0
            with self.subTest(view=view), patch.dict(os.environ, {
                "USERPROFILE": str(self.folder), "LOCALAPPDATA": str(self.folder / view)
            }), patch.object(desktop_app, "read_device_config", wraps=desktop_app.read_device_config) as read, \
                 patch("PySide6.QtCore.QLockFile", return_value=lock) as lock_type, \
                 patch("PySide6.QtCore.QTimer"), \
                 patch("PySide6.QtNetwork.QLocalServer", return_value=server), \
                 patch("PySide6.QtWidgets.QApplication", return_value=app), \
                 patch("PySide6.QtWidgets.QMessageBox.warning"), \
                 patch("time_logger.desktop_controller.DesktopController", return_value=controller) as controller_type, \
                 patch("time_logger.desktop_ui.DesktopWindow", return_value=window) as window_type, \
                 patch("time_logger.desktop_windows.GlobalShortcut", return_value=shortcut):
                self.assertEqual(desktop_app.main(["--background"]), 0)
            read.assert_called_once_with(expected / "device.json")
            lock_type.assert_called_once_with(str(expected / "desktop.lock"))
            controller_type.assert_called_once_with({"fixture": True}, expected / "state" / "desktop.sqlite3")
            settings = window_type.call_args.kwargs["settings"]
            self.assertIsNotNone(settings)
            self.assertEqual(Path(settings.fileName()), expected / "ui.ini")
            self.assertTrue((expected / "runtime-health.json").is_file())

    def _existing_instance(self, connected, args=None):
        lock = Mock()
        lock.tryLock.return_value = False
        with patch.object(desktop_app, "data_directory", return_value=self.folder), \
             patch.object(desktop_app, "read_device_config", create=True) as read, \
             patch.object(desktop_app, "activate_existing_instance", return_value=connected, create=True) as activate, \
             patch("PySide6.QtCore.QLockFile", return_value=lock), \
             patch("PySide6.QtWidgets.QApplication", return_value=APP), \
             patch("PySide6.QtWidgets.QMessageBox.warning") as warning:
            result = desktop_app.main(args or [])
        return result, read, activate, warning

    def test_existing_instance_is_activated_before_any_configuration_read(self):
        result, read, activate, warning = self._existing_instance(True)
        self.assertEqual(result, 0)
        read.assert_not_called()
        activate.assert_called_once()
        warning.assert_not_called()

    def test_live_ipc_is_probed_before_lock_or_configuration(self):
        lock = Mock()
        with patch.object(desktop_app, "data_directory", return_value=self.folder), \
             patch.object(desktop_app, "read_device_config") as read, \
             patch.object(desktop_app, "activate_existing_instance", return_value=True), \
             patch("PySide6.QtCore.QLockFile", return_value=lock), \
             patch("PySide6.QtWidgets.QApplication", return_value=APP):
            self.assertEqual(desktop_app.main([]), 0)
        lock.tryLock.assert_not_called()
        read.assert_not_called()

    def test_failed_activation_reports_failure_without_reading_configuration(self):
        result, read, activate, warning = self._existing_instance(False)
        self.assertEqual(result, 1)
        read.assert_not_called()
        self.assertEqual(activate.call_count, 2)
        self.assertIn("唤起", warning.call_args.args[2])
        self.assertNotIn("未绑定", warning.call_args.args[2])

    def test_background_existing_instance_does_not_open_or_read_configuration(self):
        result, read, activate, warning = self._existing_instance(True, ["--background"])
        self.assertEqual(result, 0)
        read.assert_not_called()
        activate.assert_not_called()
        warning.assert_not_called()

    def test_config_missing_has_specific_safe_diagnostic(self):
        config, diagnostic = desktop_app.read_device_config(self.config_path)
        self.assertIsNone(config)
        self.assertEqual(diagnostic["category"], "config_missing")
        self.assertEqual(diagnostic["errno"], errno.ENOENT)
        self.assertEqual(diagnostic["path"], str(self.config_path))
        self.assertIn(diagnostic["winerror"], (None, 2, 3))
        self.assertEqual(diagnostic["attempts"], 3)

    def test_temporary_missing_same_path_is_retried_without_rebinding(self):
        failure = FileNotFoundError(errno.ENOENT, "missing")
        with patch.object(Path, "read_text", side_effect=[failure, '{"fixture": true}']) as read, \
             patch.object(desktop_app.time, "sleep") as sleep:
            config, diagnostic = desktop_app.read_device_config(self.config_path)
        self.assertEqual(config, {"fixture": True})
        self.assertIsNone(diagnostic)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(.15)

    def test_config_unreadable_does_not_log_exception_text_or_data(self):
        failure = PermissionError(errno.EACCES, "PRIVATE_EXCEPTION_TEXT")
        with patch.object(Path, "read_text", side_effect=failure):
            config, diagnostic = desktop_app.read_device_config(self.config_path)
        self.assertIsNone(config)
        self.assertEqual(diagnostic["category"], "config_unreadable")
        self.assertEqual(diagnostic["errno"], errno.EACCES)
        desktop_app.save_startup_error(self.folder, diagnostic)
        saved = (self.folder / "startup-error.json").read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(saved)), {"category", "errno", "winerror", "path", "attempts",
                                                 "pid", "executable", "background", "capture", "config_override"})
        self.assertNotIn("PRIVATE_EXCEPTION_TEXT", saved)

    def test_startup_diagnostic_context_drops_unapproved_keys(self):
        desktop_app.save_startup_error(self.folder, {"category": "config_missing", "pid": 123,
                                      "executable": "C:/fixture.exe", "capture": False,
                                      "argv": "PRIVATE_ARG", "config": "PRIVATE_CONFIG"})
        saved = json.loads((self.folder / "startup-error.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["pid"], 123)
        self.assertEqual(saved["executable"], "C:/fixture.exe")
        self.assertNotIn("PRIVATE", json.dumps(saved))

    def test_invalid_json_or_encoding_is_distinct_from_missing_binding(self):
        for raw in (b'{"PRIVATE_CONTENT": invalid}', b'\xff\xfeinvalid', b'[]'):
            with self.subTest(raw=raw):
                self.config_path.write_bytes(raw)
                config, diagnostic = desktop_app.read_device_config(self.config_path)
                self.assertIsNone(config)
                self.assertEqual(diagnostic["category"], "config_invalid")
                self.assertNotIn("PRIVATE_CONTENT", json.dumps(diagnostic))

    def test_utf8_bom_is_accepted(self):
        self.config_path.write_text('{"fixture": "synthetic only"}', encoding="utf-8-sig")
        config, diagnostic = desktop_app.read_device_config(self.config_path)
        self.assertEqual(config, {"fixture": "synthetic only"})
        self.assertIsNone(diagnostic)

    def test_ipc_uses_connection_request_without_racing_old_server_close(self):
        for connected in (True, False):
            with self.subTest(connected=connected):
                socket = Mock()
                socket.waitForConnected.return_value = connected
                with patch("PySide6.QtNetwork.QLocalSocket", return_value=socket):
                    self.assertEqual(desktop_app.activate_existing_instance("fixture-instance"), connected)
                socket.connectToServer.assert_called_once_with("fixture-instance")
                socket.write.assert_not_called()

    def test_configuration_failure_releases_new_instance_lock_and_explains_category(self):
        lock = Mock()
        lock.tryLock.return_value = True
        diagnostic = {"category": "config_unreadable", "errno": errno.EACCES,
                      "winerror": 5, "path": str(self.config_path)}
        with patch.object(desktop_app, "data_directory", return_value=self.folder), \
             patch.object(desktop_app, "activate_existing_instance", return_value=False), \
             patch.object(desktop_app, "read_device_config", return_value=(None, diagnostic), create=True), \
             patch.object(desktop_app, "save_startup_error", create=True) as save, \
             patch("PySide6.QtCore.QLockFile", return_value=lock), \
             patch("PySide6.QtWidgets.QApplication", return_value=APP), \
             patch("PySide6.QtWidgets.QMessageBox.warning") as warning:
            self.assertEqual(desktop_app.main([]), 1)
        lock.unlock.assert_called_once()
        save.assert_called_once()
        saved = save.call_args.args[1]
        self.assertEqual(saved["category"], diagnostic["category"])
        self.assertEqual(saved["path"], str(self.config_path))
        self.assertEqual(saved["pid"], os.getpid())
        self.assertFalse(saved["capture"])
        self.assertIn("无法读取", warning.call_args.args[2])

    def test_first_run_cancel_does_not_create_a_queue_or_credential(self):
        lock, dialog = Mock(), Mock()
        lock.tryLock.return_value = True
        dialog.exec.return_value = 0
        with patch.object(desktop_app, "data_directory", return_value=self.folder), \
             patch.object(desktop_app, "activate_existing_instance", return_value=False), \
             patch("PySide6.QtCore.QLockFile", return_value=lock), \
             patch("PySide6.QtWidgets.QApplication", return_value=APP), \
             patch("time_logger.desktop_setup.SetupDialog", return_value=dialog) as dialog_type:
            dialog_type.DialogCode.Accepted = 1
            self.assertEqual(desktop_app.main([]), 0)
        dialog_type.assert_called_once_with(self.folder / "device.json")
        self.assertFalse((self.folder / "state" / "desktop.sqlite3").exists())
        lock.unlock.assert_called_once()

    def test_existing_queue_with_missing_config_does_not_offer_rebinding(self):
        state = self.folder / "state"
        state.mkdir()
        (state / "desktop.sqlite3").write_bytes(b"preserved-existing-state")
        lock = Mock()
        lock.tryLock.return_value = True
        with patch.object(desktop_app, "data_directory", return_value=self.folder), \
             patch.object(desktop_app, "activate_existing_instance", return_value=False), \
             patch("PySide6.QtCore.QLockFile", return_value=lock), \
             patch("PySide6.QtWidgets.QApplication", return_value=APP), \
             patch("PySide6.QtWidgets.QMessageBox.warning") as warning, \
             patch("time_logger.desktop_setup.SetupDialog") as setup:
            self.assertEqual(desktop_app.main([]), 1)
        setup.assert_not_called()
        self.assertIn("恢复配置", warning.call_args.args[2])
        self.assertEqual((state / "desktop.sqlite3").read_bytes(), b"preserved-existing-state")


if __name__ == "__main__":
    unittest.main()
