"""Native visibility correction without opening the user's application."""
import unittest
from unittest.mock import Mock, patch

from time_logger import desktop_windows


class NativeVisibilityTests(unittest.TestCase):
    def call_with(self, *, platform="windows", operating_system="nt", exists=True,
                  visibility=(False, True), show_result=0):
        user = Mock()
        user.IsWindow.return_value = exists
        user.IsWindowVisible.side_effect = visibility
        user.ShowWindow.return_value = show_result
        with patch.object(desktop_windows.os, "name", operating_system), \
             patch.object(desktop_windows, "QGuiApplication") as gui, \
             patch.object(desktop_windows.ctypes, "WinDLL", return_value=user) as library:
            gui.platformName.return_value = platform
            result = desktop_windows.ensure_native_visible(0x123456789)
        return result, user, library

    def test_non_windows_does_not_call_native_api(self):
        result, _, library = self.call_with(operating_system="posix")
        self.assertIsNone(result)
        library.assert_not_called()

    def test_offscreen_does_not_call_native_api(self):
        result, _, library = self.call_with(platform="offscreen")
        self.assertIsNone(result)
        library.assert_not_called()

    def test_invalid_window_is_not_shown(self):
        result, user, _ = self.call_with(exists=False)
        self.assertFalse(result)
        user.IsWindowVisible.assert_not_called()
        user.ShowWindow.assert_not_called()

    def test_visible_window_is_not_changed(self):
        result, user, _ = self.call_with(visibility=(True, True))
        self.assertTrue(result)
        user.ShowWindow.assert_not_called()

    def test_hidden_window_is_shown_and_result_comes_from_visibility(self):
        result, user, _ = self.call_with(show_result=0)
        self.assertTrue(result)
        user.ShowWindow.assert_called_once_with(0x123456789, 5)
        self.assertEqual(user.IsWindowVisible.call_count, 2)

    def test_native_show_that_remains_hidden_is_not_reported_as_success(self):
        result, user, _ = self.call_with(visibility=(False, False), show_result=1)
        self.assertFalse(result)
        user.ShowWindow.assert_called_once()


if __name__ == "__main__":
    unittest.main()
