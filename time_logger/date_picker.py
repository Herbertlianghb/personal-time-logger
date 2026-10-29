"""The selected dual-column calendar, retaining QDateEdit's browsing contract."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QLocale, Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget, QDateEdit, QFrame, QHBoxLayout, QLabel, QPushButton,
    QTableView, QToolButton, QVBoxLayout, QWidget,
)

from .timeline_widget import TAIPEI


def today_in_taipei():
    value = datetime.now(TAIPEI).date()
    return QDate(value.year, value.month, value.day)


class DualColumnCalendar(QCalendarWidget):
    """Quick dates on a quiet sidebar beside the native month view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dualColumnCalendar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setLocale(QLocale("zh_CN"))
        self.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setNavigationBarVisible(False)
        self.setGridVisible(False)
        self.setFixedWidth(408)
        self.setMinimumHeight(332)
        self.setAccessibleName("选择历史日期")
        self.setStyleSheet("""
            QCalendarWidget#dualColumnCalendar { background: #ffffff; border: 1px solid #dfe4e9; border-radius: 13px; }
            QCalendarWidget#dualColumnCalendar QWidget { color: #20252b; }
            QCalendarWidget QTableView { background: #ffffff; alternate-background-color: #ffffff; border: 0; outline: 0; selection-background-color: #295b50; }
            QLabel#calendarMonth { color: #20252b; font-size: 14px; font-weight: 500; }
            QToolButton#calendarArrow { background: #ffffff; color: #606975; border: 0; border-radius: 7px; padding: 0; font-size: 21px; min-height: 0; }
            QToolButton#calendarArrow:hover { background: #f3f6f4; }
            QToolButton#calendarArrow:focus { border: 1px solid #295b50; }
            QToolButton#calendarArrow:disabled { color: #bdc3c0; }
            QFrame#calendarQuickSidebar { background: #f6f8f6; border: 0; border-right: 1px solid #e7ece8; border-top-left-radius: 12px; border-bottom-left-radius: 12px; }
            QLabel#calendarQuickCaption { background: transparent; color: #606975; font-size: 12px; }
            QPushButton#calendarQuick { background: transparent; border: 0; border-radius: 8px; padding: 0; min-height: 52px; max-height: 52px; }
            QPushButton#calendarQuick:hover { background: #eaf0ec; }
            QPushButton#calendarQuick:focus { border: 1px solid #295b50; }
            QLabel#calendarQuickTitle { background: transparent; color: #20252b; font-size: 14px; }
            QLabel#calendarQuickDate { background: transparent; color: #78807d; font-size: 11px; }
            QPushButton#calendarQuick[chosen="true"] { background: #295b50; }
            QLabel#calendarQuickTitle[chosen="true"] { color: #ffffff; }
            QLabel#calendarQuickDate[chosen="true"] { color: #d6e4df; }
            QLabel#calendarQuickTitle:disabled, QLabel#calendarQuickDate:disabled { color: #b0b7b2; }
            QFrame#calendarFooter { border: 0; border-top: 1px solid #edf0ed; }
            QLabel#calendarSelection { color: #606975; font-size: 12px; }
        """)
        palette = self.palette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button,
                     QPalette.ColorRole.AlternateBase):
            palette.setColor(role, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#20252b"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#20252b"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#295b50"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        self.setPalette(palette)
        weekday = QTextCharFormat()
        weekday.setForeground(QColor("#606975"))
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(12)
        weekday.setFont(font)
        self.setHeaderTextFormat(weekday)
        for day in range(1, 8):
            self.setWeekdayTextFormat(Qt.DayOfWeek(day), weekday)

        self.quick_sidebar = QFrame(self)
        self.quick_sidebar.setObjectName("calendarQuickSidebar")
        self.quick_sidebar.setFixedWidth(104)
        side = QVBoxLayout(self.quick_sidebar)
        side.setContentsMargins(10, 17, 10, 14)
        side.setSpacing(8)
        caption = QLabel("快捷查看")
        caption.setObjectName("calendarQuickCaption")
        caption.setContentsMargins(8, 0, 0, 7)
        side.addWidget(caption)
        self.quick_buttons, self.quick_date_labels = [], []
        self.quick_dates = []
        for index, text in enumerate(("今天", "昨天", "前天")):
            button = QPushButton()
            button.setObjectName("calendarQuick")
            button.setFixedHeight(52)
            labels = QVBoxLayout(button)
            labels.setContentsMargins(10, 6, 6, 6)
            labels.setSpacing(1)
            title = QLabel(text)
            title.setObjectName("calendarQuickTitle")
            date_label = QLabel()
            date_label.setObjectName("calendarQuickDate")
            for label in (title, date_label):
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                labels.addWidget(label)
            button.clicked.connect(lambda checked=False, offset=index: self.choose_relative(offset))
            self.quick_buttons.append(button)
            self.quick_date_labels.append(date_label)
            side.addWidget(button)
        side.addStretch(1)

        header = QWidget()
        month_row = QHBoxLayout(header)
        month_row.setContentsMargins(0, 0, 0, 0)
        month_row.setSpacing(2)
        self.month_label = QLabel()
        self.month_label.setObjectName("calendarMonth")
        month_row.addWidget(self.month_label)
        month_row.addStretch(1)
        self.previous_button = self._arrow("‹", "上个月")
        self.next_button = self._arrow("›", "下个月")
        self.previous_button.clicked.connect(self.showPreviousMonth)
        self.next_button.clicked.connect(self.showNextMonth)
        month_row.addWidget(self.previous_button)
        month_row.addWidget(self.next_button)

        footer = QFrame()
        footer.setObjectName("calendarFooter")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(0, 10, 0, 0)
        self.selection_label = QLabel()
        self.selection_label.setObjectName("calendarSelection")
        bottom.addWidget(self.selection_label)
        layout = self.layout()
        # Keep Qt's native date table and popup acceptance wiring intact. The
        # left margin reserves the sidebar without reparenting the date view.
        layout.setContentsMargins(118, 16, 13, 11)
        layout.setSpacing(10)
        layout.insertWidget(0, header)
        layout.addWidget(footer)
        view = self.findChild(QTableView)
        if view:
            view.setShowGrid(False)
            view.setMinimumWidth(0)
            view.setFixedHeight(224)
            view.verticalHeader().setMinimumSectionSize(28)
            view.verticalHeader().setDefaultSectionSize(32)
        self.currentPageChanged.connect(self._update_heading)
        self.selectionChanged.connect(self._update_selection)
        self._update_heading(self.yearShown(), self.monthShown())
        self._refresh_quick_dates()
        self._update_selection()

    @staticmethod
    def _arrow(text, label):
        button = QToolButton()
        button.setObjectName("calendarArrow")
        button.setText(text)
        button.setFixedSize(28, 28)
        button.setToolTip(label)
        button.setAccessibleName(label)
        return button

    def _update_heading(self, year, month):
        self.month_label.setText(f"{year}年{month}月")
        first = QDate(year, month, 1)
        self.previous_button.setEnabled(first.addDays(-1) >= self.minimumDate())
        self.next_button.setEnabled(first.addMonths(1) <= self.maximumDate())

    def _update_selection(self):
        chosen = self.selectedDate()
        weekday = "一二三四五六日"[chosen.dayOfWeek() - 1]
        self.selection_label.setText(f"{chosen.month()}月{chosen.day()}日 · 周{weekday}")
        for button, date in zip(self.quick_buttons, self.quick_dates):
            for widget in (button, *button.findChildren(QLabel)):
                widget.setProperty("chosen", date == chosen)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()

    def _refresh_quick_dates(self):
        today = today_in_taipei()
        self.quick_dates = [today.addDays(-index) for index in range(3)]
        for title, button, label, date in zip(("今天", "昨天", "前天"), self.quick_buttons, self.quick_date_labels, self.quick_dates):
            label.setText(f"{date.month()}月{date.day()}日")
            button.setAccessibleName(f"{title} {date.toString('yyyy-MM-dd')}")
            button.setToolTip(date.toString("yyyy-MM-dd"))
            button.setEnabled(self.minimumDate() <= date <= self.maximumDate())
        self._update_selection()

    def showEvent(self, event):
        self._refresh_quick_dates()
        self._update_heading(self.yearShown(), self.monthShown())
        super().showEvent(event)

    def resizeEvent(self, event):
        self.quick_sidebar.setGeometry(1, 1, 104, self.height() - 2)
        super().resizeEvent(event)

    def choose_relative(self, index):
        self._refresh_quick_dates()
        date = self.quick_dates[index]
        if not self.minimumDate() <= date <= self.maximumDate():
            return
        self.setSelectedDate(date)
        self.showSelectedDate()
        # QDateEdit's native popup owns acceptance/closing, including keyboard
        # activation. Emit the same signal as choosing a calendar cell.
        self.clicked.emit(date)

    def paintCell(self, painter: QPainter, rect, date):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect, QColor("#ffffff"))
        selected = date == self.selectedDate()
        enabled = self.minimumDate() <= date <= self.maximumDate()
        outside = date.month() != self.monthShown() or date.year() != self.yearShown()
        if selected:
            side = min(32, rect.width() - 4, rect.height() - 4)
            shape = QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#295b50"))
            painter.drawRoundedRect(shape, side / 2, side / 2)
        painter.setPen(QColor("#ffffff" if selected else "#a6acb2" if outside or not enabled else "#20252b"))
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(14)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(date.day()))
        painter.restore()


class DualColumnDateEdit(QDateEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setCalendarWidget(DualColumnCalendar(self))
        self.setAccessibleName("历史日期")

