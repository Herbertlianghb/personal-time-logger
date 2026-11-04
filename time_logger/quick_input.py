"""Independent pinned composer over the main window's existing draft document."""
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from .desktop_windows import ensure_native_visible


def pin_icon(pinned):
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(12, 12)
    painter.rotate(0 if pinned else 35)
    painter.translate(-12, -12)
    color = QColor("#295b50" if pinned else "#87908a")
    painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(color if pinned else Qt.BrushStyle.NoBrush)
    head = QPainterPath()
    head.moveTo(7, 4)
    for x, y in ((17, 4), (17, 7), (15, 7), (15, 10), (18, 14),
                 (6, 14), (9, 10), (9, 7), (7, 7)):
        head.lineTo(x, y)
    head.closeSubpath()
    painter.drawPath(head)
    painter.drawLine(12, 14, 12, 21)
    painter.end()
    return QIcon(pixmap)


class QuickInputWindow(QWidget):
    def __init__(self, main):
        from .desktop_ui import RecordEdit, STYLE

        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.main = main
        self.settings = main.settings
        self._shutting_down = False
        self._drag_offset = None
        self.setWindowTitle("随手记 · 输入小窗")
        self.setWindowIcon(main.windowIcon())
        self.setAccessibleName("随手记独立输入小窗")
        self.setPalette(main.palette())
        self.setStyleSheet(STYLE)
        self.setMinimumSize(300, 210)
        self.resize(380, 240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 10)
        layout.setSpacing(4)
        self.header = QWidget()
        self.header.setFixedHeight(20)
        self.header.setCursor(Qt.CursorShape.OpenHandCursor)
        self.header.installEventFilter(self)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("随手记")
        title.setStyleSheet("font-size: 11px; color: #606975;")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(title)
        header.addStretch()
        self.pin = QToolButton()
        self.pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pin.setCheckable(True)
        self.pin.setFixedSize(20, 20)
        self.pin.setIconSize(QSize(16, 16))
        self.pin.setStyleSheet("QToolButton { min-height: 0; padding: 0; border: 1px solid transparent; border-radius: 4px; }"
                               "QToolButton:checked { background: #e5efe9; border-color: #b8cec0; }")
        self.pin.toggled.connect(self._pin_changed)
        header.addWidget(self.pin)
        layout.addWidget(self.header)
        self.context_frame = QWidget()
        context = QHBoxLayout(self.context_frame)
        context.setContentsMargins(0, 0, 0, 0)
        self.context_label = QLabel()
        self.context_label.setWordWrap(True)
        context.addWidget(self.context_label, 1)
        cancel = QToolButton()
        cancel.setText("取消关联")
        cancel.clicked.connect(main.clear_context)
        context.addWidget(cancel)
        layout.addWidget(self.context_frame)
        self.editor = RecordEdit()
        self.editor.setDocument(main.editor.document())
        self.editor.setMinimumHeight(90)
        self.editor.submit_requested.connect(self.submit)
        self.editor.textChanged.connect(self._refresh)
        self.editor.composition_changed.connect(self._refresh)
        self.editor.composition_changed.connect(main._update_submit_enabled)
        layout.addWidget(self.editor, 1)
        footer = QHBoxLayout()
        self.save_state = QLabel()
        self.save_state.setObjectName("saveState")
        self.save_state.setWordWrap(True)
        footer.addWidget(self.save_state, 1)
        self.record_button = QPushButton("记录")
        self.record_button.setObjectName("record")
        self.record_button.clicked.connect(self.submit)
        footer.addWidget(self.record_button)
        layout.addLayout(footer)
        main.editor.textChanged.connect(self._refresh)
        main.editor.composition_changed.connect(self._refresh)
        main.draft_timer.timeout.connect(self._refresh)
        main.controller.state.connect(self._refresh)
        main.context_changed.connect(self._refresh)
        main.quit_requested.connect(self.shutdown)
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        geometry = self.settings.value("quick_input/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            screen = QApplication.primaryScreen()
            if screen:
                area = screen.availableGeometry()
                self.move(area.right() - self.width() - 24, area.top() + 60)
        self.pin.setChecked(self.settings.value("quick_input/pinned", True, type=bool))
        self._pin_changed(self.pin.isChecked())
        self._refresh()

    def eventFilter(self, watched, event):
        if watched is self.header:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = None
                return True
        return super().eventFilter(watched, event)

    def _refresh(self, *_):
        self.save_state.setText(self.main.save_state.text())
        self.context_label.setText(self.main.context_label.text())
        self.context_frame.setVisible(bool(self.main._context))
        self.record_button.setEnabled(bool(self.editor.toPlainText().strip())
                                      and not self.editor.composing and not self.main.editor.composing)

    def _pin_changed(self, pinned):
        visible, active, geometry = self.isVisible(), self.isActiveWindow(), self.geometry()
        self.settings.setValue("quick_input/pinned", bool(pinned))
        self.pin.setIcon(pin_icon(pinned))
        self.pin.setAccessibleName("取消钉选" if pinned else "钉选输入小窗")
        self.pin.setToolTip("已钉选，始终置顶；点击取消" if pinned else "钉选后始终显示在其他窗口上方")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(pinned))
        self.setGeometry(geometry)
        if visible:
            self.show_input(focus=active)

    def show_input(self, *, focus=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not focus)
        self.show()
        ensure_native_visible(int(self.winId()), activate=focus)
        self.settings.setValue("quick_input/open", True)
        self._refresh()
        if focus:
            self.raise_()
            self.activateWindow()
            self.editor.setFocus()

    def submit(self):
        if self.editor.composing:
            return
        saved = self.main.submit(focus_editor=False)
        self._refresh()
        if saved:
            self.editor.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self.main.controller, "set_quick_input_visible"):
            self.main.controller.set_quick_input_visible(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self.main.controller, "set_quick_input_visible"):
            self.main.controller.set_quick_input_visible(False)

    def closeEvent(self, event):
        if not self._shutting_down:
            if self.editor.composing:
                self.save_state.setText("输入法正在选字，完成后可关闭")
                event.ignore()
                return
            if not self.main._save_draft():
                self._refresh()
                event.ignore()
                return
            self.settings.setValue("quick_input/open", False)
        self.settings.setValue("quick_input/geometry", self.saveGeometry())
        event.accept()

    def shutdown(self):
        if self._shutting_down:
            return
        self.main._save_draft()
        self.settings.setValue("quick_input/open", self.isVisible())
        self._shutting_down = True
        self.close()
