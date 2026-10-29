"""Small, read-only record cards for the timeline's category labels."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from .timeline_widget import clock, palette_tokens


def _text(value, fallback="未指定", limit=500):
    text = str(value) if value is not None else ""
    text = text.strip()
    return (text[:limit] + "…" if len(text) > limit else text) or fallback


class TimelineHoverCard(QFrame):
    """A non-activating tooltip that may be entered to read longer records."""

    MAX_RECORDS = 4

    def __init__(self, parent=None):
        flags = (Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("timelineHoverCard")
        self.setAccessibleName("时间线具体记录")
        colors = palette_tokens(False)
        self.setStyleSheet("""
            QFrame#timelineHoverCard { background: $surface; border: 1px solid $line; border-radius: 9px; }
            QFrame#hoverRecord { background: $surface; border: 0; border-top: 1px solid $line; }
            QWidget#hoverContents { background: $surface; }
            QLabel { color: $ink; font-family: 'Microsoft YaHei UI'; font-size: 12px; background: transparent; border: 0; }
            QLabel#hoverHeading { color: $primary; font-weight: 600; }
            QLabel#hoverName { font-weight: 500; }
            QLabel#hoverMeta { color: $muted; }
            QScrollArea { background: $surface; border: 0; }
            QScrollBar:vertical { background: $surface; width: 8px; }
            QScrollBar::handle:vertical { background: #a5aaaf; border-radius: 4px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """.replace("$surface", colors["surface"]).replace("$line", colors["line"])
             .replace("$ink", colors["ink"]).replace("$primary", colors["primary"])
             .replace("$muted", colors["muted"]))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(7)
        self.heading = self._label("具体记录", "hoverHeading")
        layout.addWidget(self.heading)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.contents = QWidget()
        self.contents.setObjectName("hoverContents")
        self.records_layout = QVBoxLayout(self.contents)
        self.records_layout.setContentsMargins(0, 0, 0, 0)
        self.records_layout.setSpacing(8)
        self.scroll.setWidget(self.contents)
        layout.addWidget(self.scroll)
        self._tag_rect = QRect()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(150)
        self._hide_timer.timeout.connect(self._hide_if_left)

    @staticmethod
    def _label(text, name="hoverMeta"):
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        return label

    def _clear_records(self):
        while self.records_layout.count():
            item = self.records_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_record(self, item):
        record = QFrame()
        record.setObjectName("hoverRecord")
        layout = QVBoxLayout(record)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._label(_text(item.get("name"), "未命名活动"), "hoverName"))
        layout.addWidget(self._label("一级分类  " + _text(item.get("category"))))
        layout.addWidget(self._label("二级项目  " + _text(item.get("project"))))
        start, end = item.get("start_ms"), item.get("end_ms")
        if type(start) is int or type(end) is int:
            label = "记录边界（约）" if item.get("estimated") else "记录边界"
            layout.addWidget(self._label(f"{label}  {clock(start, True)} → {clock(end, True)}"))
        elif type(item.get("report_ms")) is int:
            layout.addWidget(self._label(f"报告时刻  {clock(item['report_ms'], True)} · 实际起止未定"))
        else:
            layout.addWidget(self._label("实际起止未定"))
        estimate = item.get("display_estimate")
        if not estimate:
            estimate = next((window for window in item.get("estimated_intervals", [])
                             if isinstance(window, dict) and window.get("kind") == "sleep_report_window"), None)
        if isinstance(estimate, dict):
            a, b = estimate.get("start_ms"), estimate.get("end_ms")
            if type(a) is int and type(b) is int and a < b:
                layout.addWidget(self._label(f"睡眠（约）  {clock(a, True)} → {clock(b, True)}"))
                layout.addWidget(self._label("依据睡前与醒来报告估计，非实测睡眠，不计主动人工投入。"))
        self.records_layout.addWidget(record)

    def show_records(self, global_pos, items, global_rect):
        """Display original activity dictionaries without fetching or rewriting them."""
        items = [item for item in items if isinstance(item, dict)]
        if not items:
            self.dismiss()
            return
        self._hide_timer.stop()
        self._tag_rect = QRect(global_rect)
        self._clear_records()
        self.heading.setText("具体记录" if len(items) == 1 else f"具体记录 · 共 {len(items)} 条")
        for item in items[:self.MAX_RECORDS]:
            self._add_record(item)
        if len(items) > self.MAX_RECORDS:
            self.records_layout.addWidget(self._label(f"另有 {len(items) - self.MAX_RECORDS} 条，请在“查看节点”中选择。"))

        position = QPoint(global_pos)
        screen = QGuiApplication.screenAt(self._tag_rect.center()) or QGuiApplication.screenAt(position)
        screen = screen or QGuiApplication.primaryScreen()
        available = screen.availableGeometry().adjusted(8, 8, -8, -8) if screen else QRect(position, self.sizeHint())
        width = min(338, available.width())
        self.setFixedWidth(max(1, width))
        # QLabel's wrapped height is derived at the actual card width. The
        # scroll area contains long titles without extending beyond the screen.
        content_width = max(1, width - 34)
        content_height = self.records_layout.heightForWidth(content_width)
        if content_height < 0:
            content_height = self.records_layout.sizeHint().height()
        self.scroll.setFixedHeight(max(1, min(content_height + 2, available.height() - 68, 420)))
        self.adjustSize()
        x = max(available.left(), min(self._tag_rect.left(), available.right() - self.width() + 1))
        y = self._tag_rect.bottom() + 6
        if y + self.height() > available.bottom() + 1:
            y = self._tag_rect.top() - self.height() - 6
        y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)
        self.show()

    def schedule_hide(self):
        self._hide_timer.start()

    def _hide_if_left(self):
        position = QCursor.pos()
        if not self._tag_rect.contains(position) and not self.frameGeometry().contains(position):
            self.hide()

    def dismiss(self):
        self._hide_timer.stop()
        self._tag_rect = QRect()
        self.hide()

    def enterEvent(self, event):
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.schedule_hide()
        super().leaveEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.WindowDeactivate:
            # A tooltip never owns the keyboard; another application taking
            # focus should not leave a stale record floating above it.
            self.dismiss()
        return super().event(event)
