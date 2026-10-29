"""Small native charts for the same bounded, read-only heatmap statistics."""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QBrush, QPainter, QPen
from PySide6.QtWidgets import QWidget, QToolTip

from .timeline_widget import duration

LEVELS = ("#edf4f0", "#c6dfd2", "#8cbaa5", "#518b73", "#295b50")
ESTIMATE_COLOR = "#bc8d4b"


def _estimate_tip(item):
    estimated = item.get("estimated_seconds") or 0
    if estimated:
        return f"\n含估算区间 {duration(estimated)}（与确定区间重叠已去重），不等于净人工"
    if item.get("estimated_count") or item.get("estimated_days"):
        return "\n有估算记录，其区间已被确定区间覆盖，不重复累计"
    return ""


def _daily_tip(item):
    value = item.get("seconds")
    if item.get("future"):
        text = "未来日期"
    elif value is not None:
        text = ("约 " if item.get("estimated_seconds") else "") + duration(value)
    else:
        text = "有记录，起止待补" if item.get("record_count") else "无记录"
    return (f"{item['date']}\n{text}\n{item.get('record_count', 0)} 条记录 · {item.get('unknown_count', 0)} 条起止不全"
            + _estimate_tip(item))


class StatisticsChart(QWidget):
    date_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "daily"
        self.statistics = {}
        self.hits = []
        self.focus_index = 0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(260)
        self.setAccessibleName("时间记录统计图；方向键选择，回车查看日期")

    def set_statistics(self, statistics, mode):
        self.statistics, self.mode = statistics, mode
        self.focus_index = 0
        self.update()

    def _text(self, painter, rect, text, color="#606975", align=Qt.AlignmentFlag.AlignLeft):
        painter.setPen(QColor(color))
        painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        self.hits = []
        daily = self.statistics.get("daily", [])
        if not daily:
            self._text(painter, QRectF(self.rect()), "尚未读取所选范围的记录", align=Qt.AlignmentFlag.AlignCenter)
            return
        if self.mode != "daily" and not self.statistics.get("has_intervals"):
            self._text(painter, QRectF(self.rect()), "所选范围没有可计区间，暂不能计算时长。", align=Qt.AlignmentFlag.AlignCenter)
            return
        if self.mode == "daily":
            self._daily(painter, daily)
        elif self.mode == "weekly":
            self._weekly(painter, self.statistics.get("weekly", []))
        else:
            self._cumulative(painter, daily)
        if self.hasFocus() and self.hits:
            self.focus_index = min(self.focus_index, len(self.hits) - 1)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#173d32"), 2))
            painter.drawRoundedRect(self.hits[self.focus_index][0].adjusted(-2, -2, 2, 2), 3, 3)

    def _daily(self, painter, daily):
        first = date.fromisoformat(daily[0]["date"])
        start = first - timedelta(days=first.weekday())
        columns = (date.fromisoformat(daily[-1]["date"]) - start).days // 7 + 1
        gap = 4.0
        cell = min(27.0, max(7.0, (self.width() - 54) / columns - gap))
        left, top = 38.0, 38.0
        for weekday, name in ((0, "一"), (2, "三"), (4, "五"), (6, "日")):
            self._text(painter, QRectF(0, top + weekday * (cell + gap), 28, cell), name)
        months = set()
        for item in daily:
            day = date.fromisoformat(item["date"])
            column = (day - start).days // 7
            x, y = left + column * (cell + gap), top + day.weekday() * (cell + gap)
            rect = QRectF(x, y, cell, cell)
            key = (day.year, day.month)
            if key not in months:
                months.add(key)
                self._text(painter, QRectF(x, 6, 68, 23), f"{day.month}月")
            value = item.get("seconds")
            level = min(4, sum((value or 0) >= bound for bound in (7200, 14400, 28800, 43200)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#f6f7f8" if item.get("future") or value is None else LEVELS[level]))
            painter.drawRoundedRect(rect, 2.5, 2.5)
            if value is None and not item.get("future") and item.get("record_count"):
                painter.save()
                painter.setClipRect(rect)
                painter.fillRect(rect, QBrush(QColor("#cbd6cf"), Qt.BrushStyle.BDiagPattern))
                painter.restore()
            if (item.get("estimated_count") or item.get("estimated_seconds")) and value is not None:
                radius = max(1.5, min(3.0, cell / 5))
                painter.setBrush(QColor(ESTIMATE_COLOR))
                painter.setPen(QPen(QColor("#ffffff"), 0.6))
                painter.drawEllipse(QPointF(rect.right() - radius, rect.top() + radius), radius, radius)
            if not item.get("future"):
                self.hits.append((rect, item["date"], _daily_tip(item)))

    def _weekly(self, painter, weekly):
        if not weekly:
            return
        area = QRectF(40, 30, max(1, self.width() - 55), max(1, self.height() - 74))
        maximum = max((item.get("seconds") or 0 for item in weekly), default=0)
        self._text(painter, QRectF(0, 0, self.width(), 24), "每周已记录区间 · 周一开始 · 首尾周按所选范围裁切")
        painter.setPen(QPen(QColor("#dfe4e9"), 1))
        painter.drawLine(area.bottomLeft(), area.bottomRight())
        step = area.width() / len(weekly)
        last_month = None
        for index, item in enumerate(weekly):
            x = area.left() + index * step
            value = item.get("seconds")
            height = max(2.0, area.height() * (value or 0) / max(1, maximum))
            rect = QRectF(x + 1, area.bottom() - height, max(2.0, step - 3), height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#518b73" if value is not None else "#dfe4e9"))
            painter.drawRoundedRect(rect, 2, 2)
            if value and item.get("estimated_seconds"):
                estimate_height = height * item["estimated_seconds"] / value
                painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), estimate_height), QColor(ESTIMATE_COLOR))
            day = date.fromisoformat(item["start"])
            if day.month != last_month:
                last_month = day.month
                self._text(painter, QRectF(x, area.bottom() + 8, 60, 22), f"{day.month}月")
            end = (date.fromisoformat(item["end"]) - timedelta(days=1)).isoformat()
            label = ("约 " if item.get("estimated_seconds") else "") + duration(value) if value is not None else "无可计区间"
            tip = f"{item['start']} — {end}\n{label}\n有区间记录 {item['recorded_days']} 天" + _estimate_tip(item)
            if item.get("partial"):
                tip += " · 本周数据有缺口或范围不足一周"
            if not item.get("future"):
                self.hits.append((QRectF(x, area.top(), step, area.height()), item["start"], tip))
        self._text(painter, QRectF(0, area.top(), 38, 20), f"{maximum / 3600:g}h")

    def _cumulative(self, painter, daily):
        area = QRectF(84, 30, max(1, self.width() - 106), max(1, self.height() - 70))
        maximum = max((item.get("cumulative_seconds") or 0 for item in daily), default=0)
        for fraction in (0.0, 0.5, 1.0):
            y = area.bottom() - area.height() * fraction
            painter.setPen(QPen(QColor("#edf0ee"), 1))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            self._text(painter, QRectF(0, y - 11, 74, 22), f"{maximum * fraction / 3600:.1f}h", align=Qt.AlignmentFlag.AlignRight)
        previous = None
        step = area.width() / max(1, len(daily) - 1)
        for index, item in enumerate(daily):
            if item.get("future"):
                continue
            x = area.left() + index * step
            value = item.get("cumulative_seconds")
            if value is None:
                continue
            point = QPointF(x, area.bottom() - area.height() * value / max(1, maximum))
            if previous is not None:
                color = ESTIMATE_COLOR if item.get("estimated_seconds") else "#295b50"
                painter.setPen(QPen(QColor(color), 2, Qt.PenStyle.SolidLine if item.get("has_interval") else Qt.PenStyle.DashLine))
                painter.drawLine(previous, point)
            if item.get("has_interval"):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(ESTIMATE_COLOR if item.get("estimated_count") or item.get("estimated_seconds") else "#295b50"))
                painter.drawEllipse(point, 2.5, 2.5)
            previous = point
            estimated = item.get("cumulative_estimated_seconds") or 0
            tip = f"截至 {item['date']}\n已记录累计 {'约 ' if estimated else ''}{duration(value)}"
            if estimated:
                tip += f"\n其中估算区间 {duration(estimated)}（已去重），不等于净人工"
            if not item.get("has_interval"):
                tip += "\n当天无可计区间，累计只保留此前已记录值"
            self.hits.append((QRectF(x - max(2, step / 2), area.top(), max(4, step), area.height()), item["date"], tip))
        for index in (0, len(daily) // 2, len(daily) - 1):
            x = area.left() + index * step
            self._text(painter, QRectF(x - 35, area.bottom() + 9, 80, 22), daily[index]["date"][5:], align=Qt.AlignmentFlag.AlignCenter)

    def mouseMoveEvent(self, event):
        hit = next((item for item in self.hits if item[0].contains(event.position())), None)
        if hit:
            QToolTip.showText(event.globalPosition().toPoint(), hit[2], self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            for index, hit in enumerate(self.hits):
                if hit[0].contains(event.position()):
                    self.focus_index = index
                    self.date_selected.emit(hit[1])
                    return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if not self.hits:
            return super().keyPressEvent(event)
        offsets = {Qt.Key.Key_Left: -7 if self.mode == "daily" else -1,
                   Qt.Key.Key_Right: 7 if self.mode == "daily" else 1,
                   Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}
        if event.key() in offsets:
            self.focus_index = max(0, min(len(self.hits) - 1, self.focus_index + offsets[event.key()]))
            self.update()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.date_selected.emit(self.hits[self.focus_index][1])
        else:
            super().keyPressEvent(event)
