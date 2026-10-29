import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import QDate, Qt, QPoint
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QTableView, QLabel

from time_logger.date_picker import DualColumnCalendar, DualColumnDateEdit
from time_logger.desktop_ui import STYLE, load_offscreen_fonts


APP = QApplication.instance() or QApplication([])
load_offscreen_fonts()


class DatePickerTests(unittest.TestCase):
    def setUp(self):
        self.today_patch = patch("time_logger.date_picker.today_in_taipei", return_value=QDate(2026, 10, 28))
        self.today = self.today_patch.start()
        self.addCleanup(self.today_patch.stop)
        self.edit = DualColumnDateEdit()
        self.edit.setDate(QDate(2026, 10, 28))
        self.calendar = self.edit.calendarWidget()

    def tearDown(self):
        self.edit.close()
        self.edit.deleteLater()
        APP.processEvents()

    def test_dual_column_heading_and_quiet_weekdays_follow_selected_design(self):
        self.calendar.setCurrentPage(2026, 10)
        self.assertIsInstance(self.calendar, DualColumnCalendar)
        self.assertEqual(self.calendar.month_label.text(), "2026年10月")
        self.assertEqual(self.calendar.quick_sidebar.width(), 104)
        self.assertEqual(self.calendar.width(), 408)
        self.assertFalse(self.calendar.isNavigationBarVisible())
        self.assertEqual(self.calendar.firstDayOfWeek(), Qt.DayOfWeek.Monday)
        self.assertEqual(self.calendar.selection_label.text(), "10月28日 · 周三")
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.assertEqual(self.calendar.weekdayTextFormat(day).foreground().color(), QColor("#606975"))

    def test_month_arrows_cross_year_without_changing_selected_date(self):
        self.calendar.setCurrentPage(2026, 12)
        self.calendar.next_button.click()
        self.assertEqual((self.calendar.yearShown(), self.calendar.monthShown()), (2027, 1))
        self.assertEqual(self.calendar.month_label.text(), "2027年1月")
        self.calendar.previous_button.click()
        self.assertEqual((self.calendar.yearShown(), self.calendar.monthShown()), (2026, 12))
        self.assertEqual(self.edit.date(), QDate(2026, 10, 28))

    def test_qdateedit_contract_and_calendar_choice_keep_date_signal(self):
        spy = QSignalSpy(self.edit.dateChanged)
        self.edit.setDate(QDate(2026, 10, 25))
        self.assertEqual(self.edit.date().toString("yyyy-MM-dd"), "2026-10-25")
        self.assertEqual(spy.count(), 1)
        self.calendar.setSelectedDate(QDate(2026, 10, 26))
        self.calendar.clicked.emit(QDate(2026, 10, 26))
        self.assertEqual(self.edit.date(), QDate(2026, 10, 26))
        self.assertEqual(spy.count(), 2)

    def test_today_shortcut_uses_business_timezone_and_native_acceptance(self):
        with patch("time_logger.date_picker.today_in_taipei", return_value=QDate(2026, 10, 29)):
            self.calendar.choose_relative(0)
        self.assertEqual(self.edit.date(), QDate(2026, 10, 29))
        self.assertEqual(self.calendar.selectedDate(), QDate(2026, 10, 29))
        self.assertEqual(self.calendar.selection_label.text(), "10月29日 · 周四")

    def test_quick_dates_cross_month_and_year_using_real_calendar_arithmetic(self):
        self.today.return_value = QDate(2027, 1, 1)
        self.calendar._refresh_quick_dates()
        self.assertEqual(self.calendar.quick_dates, [QDate(2027, 1, 1), QDate(2026, 12, 31), QDate(2026, 12, 30)])
        self.assertEqual([label.text() for label in self.calendar.quick_date_labels], ["1月1日", "12月31日", "12月30日"])
        self.calendar.quick_buttons[1].click()
        self.assertEqual(self.edit.date(), QDate(2026, 12, 31))

    def test_popup_refreshes_quick_dates_after_midnight_and_quick_choice_closes_it(self):
        self.today.return_value = QDate(2026, 10, 29)
        self._open_dropdown()
        self.assertEqual(self.calendar.quick_date_labels[0].text(), "10月29日")
        self.assertEqual(self.calendar.quick_buttons[1].property("chosen"), True)
        self.calendar.quick_buttons[0].click()
        APP.processEvents()
        self.assertEqual(self.edit.date(), QDate(2026, 10, 29))
        self.assertFalse(self.calendar.isVisible())

    def test_quick_labels_are_readable_with_product_styles(self):
        self._open_dropdown()
        for button, label in zip(self.calendar.quick_buttons, self.calendar.quick_date_labels):
            self.assertGreaterEqual(button.height(), 52)
            self.assertGreaterEqual(label.height(), label.minimumSizeHint().height())
        image = self.calendar.grab().toImage()
        self.assertEqual(image.pixelColor(4, 300), QColor("#f6f8f6"))
        title = self.calendar.quick_buttons[0].findChildren(QLabel)[0]
        self.assertEqual(title.palette().color(QPalette.ColorRole.WindowText), QColor("#ffffff"))
        self.calendar.setSelectedDate(QDate(2026, 10, 27))
        self.assertEqual(title.palette().color(QPalette.ColorRole.WindowText), QColor("#20252b"))
        yesterday = self.calendar.quick_buttons[1].findChildren(QLabel)[0]
        self.assertEqual(yesterday.palette().color(QPalette.ColorRole.WindowText), QColor("#ffffff"))

    def test_quick_dates_respect_dateedit_range_without_clamping_to_another_day(self):
        self.edit.setDateRange(QDate(2026, 10, 27), QDate(2026, 10, 28))
        self.today.return_value = QDate(2026, 10, 29)
        self._open_dropdown()
        self.assertFalse(self.calendar.quick_buttons[0].isEnabled())
        self.assertTrue(self.calendar.quick_buttons[1].isEnabled())
        spy = QSignalSpy(self.calendar.clicked)
        self.calendar.choose_relative(0)
        self.assertEqual(spy.count(), 0)
        self.assertEqual(self.edit.date(), QDate(2026, 10, 28))
        self.calendar.quick_buttons[2].click()
        APP.processEvents()
        self.assertEqual(self.edit.date(), QDate(2026, 10, 27))
        self.assertEqual(spy.count(), 1)
        self.assertFalse(self.calendar.isVisible())

    def test_native_calendar_keyboard_navigation_is_retained(self):
        self.calendar.setSelectedDate(QDate(2026, 10, 28))
        self.calendar.show()
        view = self.calendar.findChild(QTableView)
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Left)
        self.assertEqual(self.calendar.selectedDate(), QDate(2026, 10, 27))
        self.calendar.hide()

    def _open_dropdown(self):
        self.edit.setStyleSheet(STYLE)
        self.edit.setDisplayFormat("yyyy-MM-dd")
        self.edit.setFixedWidth(113)
        self.edit.show()
        APP.processEvents()
        QTest.mouseClick(self.edit, Qt.MouseButton.LeftButton,
                        pos=QPoint(self.edit.width() - 8, self.edit.height() // 2))
        APP.processEvents()
        self.assertTrue(self.calendar.isVisible())

    def test_actual_dropdown_opens_and_keyboard_accepts_historical_date(self):
        self._open_dropdown()
        view = self.calendar.findChild(QTableView)
        QTest.keyClick(view, Qt.Key.Key_Left)
        QTest.keyClick(view, Qt.Key.Key_Return)
        APP.processEvents()
        self.assertEqual(self.edit.date(), QDate(2026, 10, 27))
        self.assertFalse(self.calendar.isVisible())

    def test_selected_cell_has_green_round_rect_and_weekend_is_neutral(self):
        self.calendar.setSelectedDate(QDate(2026, 10, 28))
        image = QImage(44, 44, QImage.Format.Format_ARGB32)
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        from PySide6.QtCore import QRect
        self.calendar.paintCell(painter, QRect(0, 0, 44, 44), QDate(2026, 10, 28))
        painter.end()
        self.assertEqual(image.pixelColor(22, 9), QColor("#295b50"))
        self.assertEqual(image.pixelColor(1, 1), QColor("#ffffff"))


if __name__ == "__main__":
    unittest.main()
