"""A quiet screen-edge entry for the existing desktop recording window."""
from __future__ import annotations

from PySide6.QtCore import QObject, QEvent, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from .desktop_windows import ensure_native_visible, suppress_native_transitions
from .desktop_transition import WindowTransition
from .timeline_widget import palette_tokens


class FloatingEntry(QWidget):
    entered = Signal()
    left = Signal()
    pressed = Signal()
    clicked = Signal()
    moved = Signal()
    released = Signal()

    def __init__(self):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(32, 64)
        self.setAccessibleName("个人时间记录悬浮入口")
        self.setToolTip("移入展开时间记录 · 点击展开/收起 · 拖动调整位置")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._press_position = None
        self._start_position = None
        self.dragging = False

    def paintEvent(self, event):
        colors = palette_tokens(False)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors["primary"]))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        painter.setPen(QPen(QColor(colors["primary_ink"]), 2,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(10, 23, 10, 41)
        for y, length in ((25, 8), (32, 5), (39, 7)):
            painter.drawLine(15, y, 15 + length, y)
        painter.end()

    def enterEvent(self, event):
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.globalPosition().toPoint()
            self._start_position = self.pos()
            self.dragging = False
            self.pressed.emit()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_position is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._press_position
        if delta.manhattanLength() >= QApplication.startDragDistance():
            self.dragging = True
        if self.dragging:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.move(self._start_position + delta)
            self.moved.emit()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._press_position is None:
            super().mouseReleaseEvent(event)
            return
        dragged = self.dragging
        self._press_position = None
        self.dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.released.emit()
        if not dragged:
            self.clicked.emit()
        event.accept()


class FloatingPanel(QObject):
    """Presentation only: submission, drafts and network stay in DesktopWindow."""

    HOVER_DELAY_MS = 250
    LEAVE_DELAY_MS = 650

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.entry = FloatingEntry()
        self.transition = WindowTransition(self)
        self.heatmap_transition = WindowTransition(self)
        self._heatmap = None
        self.enabled = window.settings.value("floating/enabled", True, type=bool)
        self.keep_open = False
        self._hover_suppressed = False
        self._started = False
        self._disposed = False
        self._screens = []
        self._hover = QTimer(self)
        self._hover.setSingleShot(True)
        self._hover.setInterval(self.HOVER_DELAY_MS)
        self._hover.timeout.connect(self._expand)
        self._leave = QTimer(self)
        self._leave.setSingleShot(True)
        self._leave.setInterval(self.LEAVE_DELAY_MS)
        self._leave.timeout.connect(self._collapse)
        self._watch = QTimer(self)
        self._watch.setInterval(150)
        self._watch.timeout.connect(self._check_pointer)
        self.entry.entered.connect(self._entry_entered)
        self.entry.left.connect(self._entry_left)
        self.entry.pressed.connect(self._hover.stop)
        self.entry.clicked.connect(self._activate)
        self.entry.moved.connect(self._position_panel)
        self.entry.released.connect(self._finish_drag)
        self.window.installEventFilter(self)

    def start(self):
        if self._started or self._disposed:
            return
        self._started = True
        saved = self.window.settings.value("floating/position")
        screen = QApplication.primaryScreen()
        if isinstance(saved, QPoint):
            self.entry.move(saved)
        elif screen:
            area = screen.availableGeometry()
            self.entry.move(area.right() - self.entry.width() + 1,
                            area.center().y() - self.entry.height() // 2)
        app = QApplication.instance()
        app.screenAdded.connect(self._screens_changed)
        app.screenRemoved.connect(self._screens_changed)
        app.aboutToQuit.connect(self.dispose)
        self._screens_changed()
        self._sync_visibility()

    def set_enabled(self, enabled):
        if self._disposed:
            return
        self.transition.cancel()
        self.heatmap_transition.cancel()
        self.enabled = bool(enabled)
        self.window.settings.setValue("floating/enabled", self.enabled)
        self._sync_visibility()
        if not self.enabled and self._started and not self.window.isVisible():
            self.window.show_and_focus()

    def set_keep_open(self, keep_open):
        if self._disposed:
            return
        self.keep_open = bool(keep_open)
        self._leave.stop()

    def _screens_changed(self, *_):
        for screen in self._screens:
            try:
                screen.availableGeometryChanged.disconnect(self._screen_geometry_changed)
            except (RuntimeError, TypeError):
                pass  # A removed screen can already have been destroyed by Qt.
        self._screens = QApplication.screens()
        for screen in self._screens:
            screen.availableGeometryChanged.connect(self._screen_geometry_changed)
        self._screen_geometry_changed()

    def _screen_geometry_changed(self, *_):
        self._dock_entry()
        self._position_panel()

    def _screen(self):
        return (QApplication.screenAt(self.entry.frameGeometry().center())
                or QApplication.primaryScreen())

    def _dock_entry(self):
        screen = self._screen()
        if not screen:
            return
        area = screen.availableGeometry()
        left = self.entry.frameGeometry().center().x() <= area.center().x()
        x = area.left() if left else area.right() - self.entry.width() + 1
        y = max(area.top(), min(self.entry.y(), area.bottom() - self.entry.height() + 1))
        self.entry.move(x, y)

    def _finish_drag(self):
        self._dock_entry()
        self.window.settings.setValue("floating/position", self.entry.pos())
        self.window.settings.sync()
        self._position_panel()

    def _position_panel(self, *, force=False):
        if (self._disposed or not self._started or not self.enabled
                or (not force and (not self.window.isVisible() or self.window.isMinimized()))):
            return
        screen = self._screen()
        if not screen:
            return
        area, entry = screen.availableGeometry(), self.entry.frameGeometry()
        # Floating mode has its own compact bounds. A previously saved normal
        # window geometry or a long content size hint must not enlarge it.
        width = min(460, max(1, area.width() - self.entry.width()))
        height = min(640 if self.window._expanded else 300, max(1, area.height() - 32))
        self.window.setMinimumSize(min(380, width), min(540 if self.window._expanded else 265, height))
        self.window.setMaximumSize(width, height)
        if self.window.width() != width or self.window.height() != height:
            self.window.resize(width, height)
        frame = self.window.frameGeometry()
        x = entry.right() + 1 if entry.center().x() <= area.center().x() else entry.left() - frame.width()
        x = max(area.left(), min(x, area.right() - frame.width() + 1))
        y = max(area.top(), min(entry.top(), area.bottom() - frame.height() + 1))
        self.window.move(x, y)

    def _sync_visibility(self):
        if self._disposed or not self._started:
            return
        self._hover.stop()
        self._leave.stop()
        if not self.enabled or getattr(self.window, "_closing", False):
            self.entry.hide()
            self._watch.stop()
            return
        self.entry.show()
        ensure_native_visible(int(self.entry.winId()), activate=False)
        if self._panel_visible() or self._heatmap_visible():
            self._watch.start()
        else:
            self._watch.stop()

    def _entry_entered(self):
        self._leave.stop()
        if self.enabled and not self._hover_suppressed and not self.entry.dragging and (
                not self.window.isVisible() or self.window.isMinimized()):
            self._hover.start()

    def attach_heatmap(self, dialog):
        if self._disposed or self._heatmap is dialog:
            return
        self._heatmap = dialog
        dialog.installEventFilter(self)

    def _panel_visible(self):
        return self.window.isVisible() and not self.window.isMinimized()

    def _heatmap_visible(self):
        return (self._heatmap is not None and self._heatmap.isVisible()
                and not self._heatmap.isMinimized())

    def _entry_left(self):
        self._hover.stop()
        self._hover_suppressed = False

    def _expand(self):
        if (self._disposed or not self.enabled or self._hover_suppressed or self.entry.dragging
                or QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or not self.entry.frameGeometry().contains(QCursor.pos())):
            return
        self.window.show_without_focus()
        self._position_panel()

    def _activate(self):
        if self._disposed or not self.enabled:
            return
        self._hover.stop()
        self._leave.stop()
        expanded = (self._panel_visible() or self._heatmap_visible()
                    or self.transition.revealing)
        if expanded:
            # Explicit clicks override keep-open, but not an ongoing input
            # method composition or an owned menu/dialog interaction.
            if self._protected(ignore_keep_open=True):
                return
            self._hover_suppressed = True
            if not self.window.hide_to_tray():
                self._hover_suppressed = False
                self._watch.stop()
            return
        self._hover_suppressed = False
        self.window.show_and_focus()
        self._position_panel()

    def reveal(self, *, focus):
        if self._disposed:
            return
        self.heatmap_transition.cancel()
        self.start()
        self._position_panel(force=True)
        if self.window.isVisible() or (focus and self.transition.running):
            self.transition.cancel()
            self.window._show_panel(focus=focus)
        elif not self.transition.running:
            self.transition.reveal(self.window, self.entry.geometry(),
                                   lambda: self.window._show_panel(focus=focus))

    def conceal(self, hide_callback):
        self._hover.stop()
        self._leave.stop()
        if self._heatmap_visible():
            # Capture the owned window before hiding its parent. reject() keeps
            # the reusable dialog and emits finished to stop visible refreshes.
            suppress_native_transitions(int(self._heatmap.winId()))
            self.heatmap_transition.conceal(
                self._heatmap, self.entry.geometry(), self._heatmap.reject)
        self.transition.conceal(self.window, self.entry.geometry(), hide_callback)

    def _protected(self, *, ignore_keep_open=False):
        editor = self.window.editor
        if ((self.keep_open and not ignore_keep_open) or self.entry.dragging or editor.composing
                or QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or QApplication.activePopupWidget() is not None
                or QApplication.activeModalWidget() is not None):
            return True
        # Heatmap participates in pointer bounds; other dialogs still protect
        # their interactions even when non-modal.
        return any(dialog is not self._heatmap and dialog.isVisible()
                   for dialog in self.window.findChildren(QDialog))

    def _pointer_inside(self):
        position = QCursor.pos()
        card = getattr(self.window, "timeline_hover", None)
        return (self.entry.frameGeometry().contains(position)
                or (self._panel_visible() and self.window.frameGeometry().contains(position))
                or (self._heatmap_visible() and self._heatmap.frameGeometry().contains(position))
                or (card is not None and card.isVisible()
                    and card.frameGeometry().contains(position)))

    def _check_pointer(self):
        if self._pointer_inside() or self._protected():
            self._leave.stop()
        elif not self._leave.isActive():
            self._leave.start()

    def _collapse(self):
        if (self.enabled and not self._disposed
                and (self._panel_visible() or self._heatmap_visible())
                and not self._pointer_inside() and not self._protected()):
            if not self.window.hide_to_tray():
                # Avoid repeatedly retrying a failed draft save in the background.
                self._watch.stop()

    def eventFilter(self, watched, event):
        if (watched is self.window or watched is self._heatmap) and not self._disposed:
            if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowStateChange):
                QTimer.singleShot(0, self._sync_visibility)
            elif watched is self.window and event.type() == QEvent.Type.Resize and self.enabled:
                QTimer.singleShot(0, self._position_panel)
        return super().eventFilter(watched, event)

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self.transition.dispose()
        self.heatmap_transition.dispose()
        self._hover.stop()
        self._leave.stop()
        self._watch.stop()
        self.window.removeEventFilter(self)
        if self._heatmap is not None:
            self._heatmap.removeEventFilter(self)
        app = QApplication.instance()
        if self._started:
            app.screenAdded.disconnect(self._screens_changed)
            app.screenRemoved.disconnect(self._screens_changed)
            for screen in self._screens:
                try:
                    screen.availableGeometryChanged.disconnect(self._screen_geometry_changed)
                except (RuntimeError, TypeError):
                    pass
        self.entry.hide()
        self.entry.deleteLater()
