"""Animate a recording-panel snapshot to and from its floating entry."""
from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QObject, QPropertyAnimation, QRect, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from .desktop_windows import ensure_native_visible, suppress_native_transitions


class _SnapshotWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._pixmap = QPixmap()

    def set_snapshot(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def clear(self):
        self.hide()
        self._pixmap = QPixmap()

    def paintEvent(self, event):
        if not self._pixmap.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            # This overload scales the pixmap and honors its device pixel ratio.
            painter.drawPixmap(self.rect(), self._pixmap)
            painter.end()


class WindowTransition(QObject):
    """Visual proxy only: the real widget keeps its geometry, layout and draft.

    Callers position a frameless panel before reveal and save its draft before
    conceal. Callbacks perform the actual show/hide without another animation.
    Cancelling drops a pending reveal, so a closed panel cannot reappear later.
    """

    DURATION_MS = 160

    def __init__(self, parent=None):
        super().__init__(parent)
        self.overlay = _SnapshotWindow()
        self.animation = QPropertyAnimation(self.overlay, b"geometry", self)
        self.animation.setDuration(self.DURATION_MS)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.finished.connect(self._finished)
        self._on_finished = None
        self._disposed = False
        self._generation = 0

    @property
    def running(self):
        return self.animation.state() == QAbstractAnimation.State.Running

    @property
    def revealing(self):
        return self.running and self._on_finished is not None

    def reveal(self, window, anchor, show_callback):
        if self._disposed:
            return
        self.cancel()
        if window.isVisible() and not window.isMinimized():
            show_callback()
            return
        pixmap = window.grab()
        target = QRect(window.geometry())
        if pixmap.isNull() or not target.isValid() or not anchor.isValid():
            show_callback()
            return
        self._prepare(pixmap, QRect(anchor), target, show_callback)
        self.animation.start()

    def conceal(self, window, anchor, hide_callback):
        if self._disposed:
            return
        self.cancel()
        if not window.isVisible() or window.isMinimized():
            hide_callback()
            return
        pixmap = window.grab()
        start = QRect(window.geometry())
        if pixmap.isNull() or not start.isValid() or not anchor.isValid():
            hide_callback()
            return
        self._prepare(pixmap, start, QRect(anchor), None)
        # Cover the live panel before hiding it; the proxy then travels to the
        # entry instead of asking Windows to animate a taskbar minimization.
        generation = self._generation
        if hide_callback() is False:
            self.cancel()
            return
        if not self._disposed and generation == self._generation:
            self.animation.start()

    def _prepare(self, pixmap, start, end, callback):
        self._on_finished = callback
        self.overlay.set_snapshot(pixmap)
        self.overlay.setGeometry(start)
        self.animation.setStartValue(start)
        self.animation.setEndValue(end)
        suppress_native_transitions(int(self.overlay.winId()))
        self.overlay.show()
        ensure_native_visible(int(self.overlay.winId()), activate=False)

    def _finished(self):
        generation = self._generation
        callback, self._on_finished = self._on_finished, None
        if not self._disposed and callback is not None:
            callback()
        # A callback may start a new transition; do not hide its new proxy.
        if not self._disposed and generation == self._generation:
            self.overlay.clear()

    def cancel(self):
        if self._disposed:
            return
        self._generation += 1
        self.animation.stop()
        self._on_finished = None
        self.overlay.clear()

    def dispose(self):
        if self._disposed:
            return
        self.cancel()
        self._disposed = True
        self.overlay.deleteLater()
