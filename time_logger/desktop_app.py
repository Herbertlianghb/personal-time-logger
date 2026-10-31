from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

VERSION = "1.4.15"


def data_directory():
    # AppData can be a merged, package-private view when launched by Codex.
    # Use the same physical location for Explorer and maintenance launches.
    return Path(os.environ["USERPROFILE"]) / ".personal-time-logger"


def read_device_config(path):
    """Return configuration or a diagnostic containing no configuration data."""
    def failed(category, exc=None):
        return None, {"category": category, "errno": getattr(exc, "errno", None),
                      "winerror": getattr(exc, "winerror", None), "path": str(path),
                      "attempts": attempt + 1}
    # A reported missing path was present and unchanged when inspected. Retry
    # only this same path briefly; never recreate a binding or search elsewhere.
    for attempt in range(3):
        try:
            text = path.read_text(encoding="utf-8-sig")
            break
        except FileNotFoundError as exc:
            if attempt == 2:
                return failed("config_missing", exc)
            time.sleep(.15)
        except UnicodeError:
            return failed("config_invalid")
        except (OSError, ValueError) as exc:
            return failed("config_unreadable", exc)
    try:
        config = json.loads(text)
    except ValueError:
        return failed("config_invalid")
    if not isinstance(config, dict):
        return failed("config_invalid")
    return config, None


def save_startup_error(folder, diagnostic):
    # Whitelist fields: exception text can contain input or configuration data.
    safe = {key: diagnostic.get(key) for key in ("category", "errno", "winerror", "path", "attempts",
                                               "pid", "executable", "background", "capture", "config_override")}
    try:
        (folder / "startup-error.json").write_text(json.dumps(safe, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # Reporting a read failure must not fail on another denied write.


def activate_existing_instance(name):
    """The existing server activates on connection, including older releases."""
    from PySide6.QtNetwork import QLocalSocket
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        return socket.waitForConnected(1500)
    finally:
        socket.disconnectFromServer()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--capture")
    parser.add_argument("--capture-calendar", action="store_true")
    parser.add_argument("--date")
    parser.add_argument("--expanded", action="store_true")
    agent = parser.add_mutually_exclusive_group()
    agent.add_argument("--agent-export")
    agent.add_argument("--agent-import")
    parser.add_argument("--agent-result")
    args = parser.parse_args(argv)
    if args.agent_export or args.agent_import:
        from .standalone_agent import run
        if args.agent_import and not args.agent_result:
            parser.error("--agent-import requires --agent-result")
        try:
            run(Path(args.config) if args.config else data_directory() / "device.json",
                export_path=args.agent_export, day=args.date,
                import_path=args.agent_import, result_path=args.agent_result)
            return 0
        except Exception:
            return 1
    if args.capture:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QLockFile, QSettings, QTimer, Qt
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .desktop_controller import DesktopController
    from .desktop_ui import DesktopWindow, application_icon
    from .desktop_windows import GlobalShortcut

    app = QApplication(sys.argv[:1])
    # The approved reference is light even when Windows uses a dark theme.
    # This request affects this Qt process only, never the OS preference.
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setApplicationName("个人时间记录")
    app.setOrganizationName("PersonalTimeLogger")
    app.setApplicationVersion(VERSION)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(application_icon())
    folder = data_directory()
    folder.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config) if args.config else folder / "device.json"
    name = "PersonalTimeLogger-" + hashlib.sha256(str(folder).encode()).hexdigest()[:16]
    lock = QLockFile(str(folder / "desktop.lock"))
    lock.setStaleLockTime(0)
    server = None
    if not args.capture:
        # Probe the live server directly, before relying on the file lock or
        # touching configuration. Background login launches remain silent.
        if not args.background and activate_existing_instance(name):
            return 0
        if not lock.tryLock(0):
            if args.background:
                return 0
            if activate_existing_instance(name):
                return 0
            QMessageBox.warning(None, "个人时间记录", "程序已在运行，但未能发送唤起请求。请稍后再次打开桌面入口。")
            return 1
    config, diagnostic = read_device_config(config_path)
    if (diagnostic and diagnostic["category"] == "config_missing" and not args.config
            and not args.capture and not args.background
            and not (folder / "state" / "desktop.sqlite3").exists()):
        from .desktop_setup import SetupDialog
        setup = SetupDialog(config_path)
        if setup.exec() != SetupDialog.DialogCode.Accepted:
            lock.unlock()
            return 0
        config, diagnostic = read_device_config(config_path)
    if diagnostic:
        save_startup_error(folder, {**diagnostic, "pid": os.getpid(), "executable": sys.executable,
                                   "background": args.background, "capture": bool(args.capture),
                                   "config_override": bool(args.config)})
        messages = {"config_missing": "未找到设备绑定配置，暂时无法启动。",
                    "config_unreadable": "设备绑定配置暂时无法读取，暂时无法启动。",
                    "config_invalid": "设备绑定配置格式错误，暂时无法启动。"}
        if not args.background:
            QMessageBox.warning(None, "个人时间记录", messages[diagnostic["category"]] +
                                "\n本机已有记录未被改动。\n配置位置：" + str(config_path) +
                                "\n请按下载包 docs/SETUP.md 恢复配置；已有记录时不会自动重新绑定。")
        if not args.capture:
            lock.unlock()
        return 1
    if not args.capture:
        server = QLocalServer()
        if not server.listen(name):
            lock.unlock()
            QMessageBox.warning(None, "个人时间记录", "无法建立程序唤起入口，暂时无法启动。请稍后重试。")
            return 1
    capture_temp = tempfile.TemporaryDirectory(prefix="ptl-capture-") if args.capture else None
    state_path = Path(capture_temp.name) / "capture.sqlite3" if capture_temp else folder / "state" / "desktop.sqlite3"
    settings_folder = Path(capture_temp.name) if capture_temp else folder
    ui_settings = QSettings(str(settings_folder / "ui.ini"), QSettings.Format.IniFormat)
    controller = DesktopController(config, state_path)
    window = DesktopWindow(controller, settings=ui_settings, create_tray=not bool(args.capture))
    shortcut = GlobalShortcut(app, window.toggle_window) if not args.capture else None
    window.set_hotkey(shortcut.label if shortcut and shortcut.label else "托盘或桌面入口")
    if server:
        def activate():
            connection = server.nextPendingConnection()
            if connection:
                connection.close()
            window.show_and_focus()
        server.newConnection.connect(activate)
    app.aboutToQuit.connect(controller.stop)
    if shortcut:
        app.aboutToQuit.connect(shortcut.close)
    if args.expanded or args.capture:
        window.set_expanded(True)
    if args.date:
        # Date selection affects browsing only; submissions always use their
        # actual local report time, independently of this control.
        from PySide6.QtCore import QDate
        window.date_edit.setDate(QDate.fromString(args.date, "yyyy-MM-dd"))
    controller.start()
    if args.capture:
        if args.date:
            controller.day = args.date
        path = Path(args.capture).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        def capture_view():
            target = window
            if args.capture_calendar:
                from PySide6.QtCore import QEvent, QPointF
                from PySide6.QtGui import QMouseEvent
                position = QPointF(window.date_edit.width() - 9, window.date_edit.height() / 2)
                for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                    event = QMouseEvent(kind, position, position, Qt.MouseButton.LeftButton,
                                        Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier)
                    QApplication.sendEvent(window.date_edit, event)
                target = window.date_edit.calendarWidget().window()
            target.grab().save(str(path))
            app.quit()
        def capture(snapshot):
            if snapshot.get("date") != controller.day or snapshot.get("cache", {}).get("local_cached"):
                return
            QTimer.singleShot(400, capture_view)
        controller.snapshot.connect(capture)
        QTimer.singleShot(90000, app.quit)
        window.show()  # offscreen platform; never activates another app
        controller.refresh(controller.day)
    elif not args.background:
        window.show_and_focus()
    if not args.capture:
        health_path = folder / "runtime-health.json"
        def runtime_health():
            value = {"version": VERSION, "pid": os.getpid(), "hotkey": shortcut.label if shortcut else None,
                     "as_of_ms": time.time_ns() // 1000000, "visible": window.isVisible(),
                     "executable": sys.executable, "window_id": int(window.winId())}
            quick = window._quick_input
            if quick is not None:
                value.update(quick_input_window_id=int(quick.winId()), quick_input_visible=quick.isVisible(),
                             quick_input_pinned=quick.pin.isChecked())
            temp = health_path.with_suffix(".tmp")
            temp.write_text(json.dumps(value), encoding="utf-8")
            temp.replace(health_path)
        runtime_health()
        timer = QTimer(app)
        timer.timeout.connect(runtime_health)
        timer.start(10000)
    result = app.exec()
    if server:
        server.close()
        lock.unlock()
    if capture_temp:
        capture_temp.cleanup()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
