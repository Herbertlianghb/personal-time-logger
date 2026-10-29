"""Small Windows integration surface; no global keyboard hook or capture."""
import ctypes
from ctypes import wintypes
import os

from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtGui import QGuiApplication


def ensure_native_visible(hwnd, *, activate=True):
    """Honor a requested show after Windows overrides Qt's first show call."""
    if os.name != "nt" or QGuiApplication.platformName() != "windows":
        return None
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    if not user.IsWindow(hwnd):
        return False
    if not user.IsWindowVisible(hwnd):
        # Hover reveals must not activate; explicit shortcut/click still may.
        user.ShowWindow(hwnd, 5 if activate else 4)  # SW_SHOW / SW_SHOWNOACTIVATE
    return bool(user.IsWindowVisible(hwnd))


def suppress_native_transitions(hwnd, suppress=True):
    """Use the app's entry animation instead of DWM's taskbar transition."""
    if os.name != "nt" or QGuiApplication.platformName() != "windows":
        return None
    dwm = ctypes.WinDLL("dwmapi")
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmSetWindowAttribute.restype = ctypes.c_long
    value = wintypes.BOOL(suppress)
    # DWMWA_TRANSITIONS_FORCEDISABLED applies to this HWND, not OS settings.
    return dwm.DwmSetWindowAttribute(hwnd, 3, ctypes.byref(value), ctypes.sizeof(value)) == 0


class GlobalShortcut(QAbstractNativeEventFilter):
    def __init__(self, app, callback):
        super().__init__()
        self.app, self.callback, self.label = app, callback, None
        self.hotkey_id = 0x50A1
        self.registered = False
        if os.name == "nt":
            for modifiers, key, label in [(0x4000 | 0x0002 | 0x0001, ord("T"), "Ctrl+Alt+T"),
                                          (0x4000 | 0x0002 | 0x0004, 0x78, "Ctrl+Shift+F9")]:
                if ctypes.windll.user32.RegisterHotKey(None, self.hotkey_id, modifiers, key):
                    self.label, self.registered = label, True
                    app.installNativeEventFilter(self)
                    break

    def nativeEventFilter(self, event_type, message):
        if self.registered and bytes(event_type) in {b"windows_generic_MSG", b"windows_dispatcher_MSG"}:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == self.hotkey_id:
                self.callback()
                return True, 0
        return False, 0

    def close(self):
        if self.registered:
            ctypes.windll.user32.UnregisterHotKey(None, self.hotkey_id)
            self.app.removeNativeEventFilter(self)
            self.registered = False
