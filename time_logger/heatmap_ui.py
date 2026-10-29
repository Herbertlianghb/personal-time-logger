"""Wide daily, weekly and cumulative views over the same read-only time source."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout,
)

from .heatmap_charts import StatisticsChart
from .heatmap_statistics import build_statistics
from .timeline_widget import VISIBLE_CATEGORIES, duration

LOCAL_TZ = timezone(timedelta(hours=8))


class HeatmapDialog(QDialog):
    month_requested = Signal(str, bool)
    day_selected = Signal(str)

    def __init__(self, day, parent=None, *, scope="year"):
        super().__init__(parent)
        self.setWindowTitle("时间统计 · 热力图")
        self.setMinimumSize(760, 620)
        self.resize(1040, 720)
        chosen = QDate.fromString(day, "yyyy-MM-dd")
        self.month = QDate(chosen.year(), chosen.month(), 1)
        self.scope, self.category, self.mode = scope, "全部", "daily"
        self.snapshot = {}
        self.statistics = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 20)
        outer.setSpacing(12)

        header = QHBoxLayout()
        heading = QLabel("时间统计")
        heading.setStyleSheet("font-size: 23px; font-weight: 600; color: #204a3c;")
        header.addWidget(heading)
        header.addStretch()
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("近一年", "year")
        self.scope_combo.addItem("本月", "month")
        self.scope_combo.setCurrentIndex(0 if scope == "year" else 1)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        header.addWidget(self.scope_combo)
        self.previous = QPushButton("‹")
        self.previous.setAccessibleName("统计结束月向前一个月")
        self.previous.clicked.connect(lambda: self.change_month(-1))
        header.addWidget(self.previous)
        self.title = QLabel()
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setMinimumWidth(108)
        header.addWidget(self.title)
        self.next = QPushButton("›")
        self.next.setAccessibleName("统计结束月向后一个月")
        self.next.clicked.connect(lambda: self.change_month(1))
        header.addWidget(self.next)
        outer.addLayout(header)

        self.range_label = QLabel()
        self.range_label.setStyleSheet("color: #606975; font-size: 12px;")
        outer.addWidget(self.range_label)
        cards = QHBoxLayout()
        cards.setSpacing(0)
        self.card_values, self.card_captions = {}, {}
        for key, title in (("total", "累计记录时长"), ("average", "有记录日日均"),
                           ("peak", "单日最多"), ("current", "最近连续有记录"),
                           ("longest", "最长连续有记录")):
            frame = QFrame()
            frame.setStyleSheet("QFrame { background: #ffffff; border: 1px solid #dfe4e9; border-radius: 4px; }")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(16, 15, 12, 13)
            layout.setSpacing(4)
            value = QLabel("—")
            value.setStyleSheet("font-size: 23px; font-weight: 600; color: #204a3c; border: 0;")
            label = QLabel(title)
            label.setStyleSheet("font-size: 12px; color: #606975; border: 0;")
            caption = QLabel("尚未读取")
            caption.setStyleSheet("font-size: 11px; color: #606975; border: 0;")
            caption.setWordWrap(True)
            layout.addWidget(value)
            layout.addWidget(label)
            layout.addWidget(caption)
            cards.addWidget(frame, 1)
            self.card_values[key], self.card_captions[key] = value, caption
        outer.addLayout(cards)

        tools = QHBoxLayout()
        self.chart_title = QLabel("时间热力图")
        self.chart_title.setStyleSheet("font-size: 17px; font-weight: 500;")
        tools.addWidget(self.chart_title)
        tools.addStretch()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for key, name in (("daily", "每日"), ("weekly", "每周"), ("cumulative", "累计")):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setChecked(key == self.mode)
            button.clicked.connect(lambda checked=False, value=key: self.set_mode(value))
            self.mode_group.addButton(button)
            self.mode_buttons[key] = button
            tools.addWidget(button)
        outer.addLayout(tools)
        self.chart = StatisticsChart()
        self.chart.date_selected.connect(self.select_date)
        outer.addWidget(self.chart, 1)
        self.legend = QLabel()
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet("color: #606975; font-size: 11px;")
        outer.addWidget(self.legend)

        filters = QHBoxLayout()
        filters.setSpacing(7)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        for name in ("全部", *VISIBLE_CATEGORIES):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setChecked(name == "全部")
            button.clicked.connect(lambda checked=False, value=name: self.set_category(value))
            self.filter_group.addButton(button)
            filters.addWidget(button)
        filters.addStretch()
        outer.addLayout(filters)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 12px; color: #606975;")
        outer.addWidget(self.summary)
        boundary = QLabel("累计已保存起止的区间并集，含单独标注的估算，排除明确 AI 执行窗口；不等同于净人工。无记录不代表实际为零，分类间重叠时不可相加。")
        boundary.setWordWrap(True)
        boundary.setStyleSheet("font-size: 11px; color: #606975;")
        outer.addWidget(boundary)
        footer = QHBoxLayout()
        self.status = QLabel("尚未读取")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #606975; font-size: 11px;")
        footer.addWidget(self.status, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(lambda: self.month_requested.emit(self.month_key(), True))
        footer.addWidget(refresh)
        outer.addLayout(footer)
        self.render()

    def month_key(self):
        return self.month.toString("yyyy-MM")

    def _scope_changed(self):
        self.scope = self.scope_combo.currentData()
        self._request_period()

    def _request_period(self):
        self.snapshot = {}
        self.set_status("正在读取所选范围的记录…")
        self.render()
        self.month_requested.emit(self.month_key(), False)

    def change_month(self, offset):
        self.month = self.month.addMonths(offset)
        self._request_period()

    def set_category(self, category):
        self.category = category
        self.render()

    def set_mode(self, mode):
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        self.render()

    def set_status(self, message):
        if message:
            self.status.setText(message)

    def receive_snapshot(self, snapshot):
        if snapshot.get("month") != self.month_key() or snapshot.get("scope", "month") != self.scope:
            return
        self.snapshot = snapshot
        cache, stamp = snapshot.get("cache", {}), snapshot.get("as_of_ms")
        when = datetime.fromtimestamp(stamp / 1000, LOCAL_TZ).strftime("%m-%d %H:%M") if stamp else "未知"
        state = "本机旧缓存" if cache.get("local_cached") else {"fresh": "已读取", "cached": "缓存", "stale": "旧缓存"}.get(cache.get("state"), "已读取")
        complete = " · 数据有缺口" if snapshot.get("completeness") != "complete" else ""
        error = " · 刷新受阻" if cache.get("refresh_error") or snapshot.get("error") else ""
        self.status.setText(f"{state} · 更新于 {when}{complete}{error}")
        self.status.setToolTip("\n".join(str(value) for value in snapshot.get("limitations", [])))
        self.render()

    def render(self):
        self.title.setText(self.month.toString("yyyy 年 M 月"))
        today = datetime.now(LOCAL_TZ).date()
        self.next.setEnabled(self.month_key() < today.strftime("%Y-%m"))
        self.previous.setEnabled(self.month.year() > 2000 or self.month.month() > 1)
        start = self.month.addMonths(-11) if self.scope == "year" else self.month
        if start < QDate(2000, 1, 1):
            start = QDate(2000, 1, 1)
        end = self.month.addMonths(1).addDays(-1)
        self.range_label.setText(f"{'近一年' if self.scope == 'year' else '本月'} · {start.toString('yyyy-MM-dd')} — {end.toString('yyyy-MM-dd')} · {self.category}")
        self.statistics = build_statistics(self.snapshot, self.category)
        stats = self.statistics
        if self.snapshot:
            approximate = "约 " if stats["estimated_seconds"] else ""
            self.card_values["total"].setText(approximate + duration(stats["total_seconds"]) if stats["has_intervals"] else "暂无")
            self.card_values["average"].setText(approximate + duration(stats["average_seconds"]) if stats["has_intervals"] else "暂无")
            self.card_values["peak"].setText(("约 " if stats["peak_estimated"] else "") + duration(stats["peak_seconds"]) if stats["has_intervals"] else "暂无")
            self.card_values["current"].setText(f"{stats['current_streak']} 天" if stats["has_intervals"] else "暂无")
            self.card_values["longest"].setText(f"{stats['longest_streak']} 天" if stats["has_intervals"] else "暂无")
            self.card_captions["total"].setText(f"{stats['recorded_days']} 天有可计区间 · 估算 {stats['estimated_count']} 条")
            self.card_values["total"].setToolTip(f"其中估算区间 {duration(stats['estimated_seconds'])}，与确定区间重叠已去重；不等于净人工。")
            self.card_captions["average"].setText("仅以有区间记录的日期为分母")
            self.card_captions["peak"].setText("、".join(stats["peak_dates"][:2]) or "没有可计区间")
            self.card_captions["peak"].setToolTip("、".join(stats["peak_dates"]))
            period_end = QDate.fromString(stats["range_end"] or "", "yyyy-MM-dd").addDays(-1).toString("yyyy-MM-dd")
            anchors = [value for value in (stats["as_of_date"], period_end) if value]
            anchor = stats["streak_end_date"] or (min(anchors) if anchors else "未知")
            self.card_captions["current"].setText("截至 " + anchor)
            self.card_captions["longest"].setText("仅限所选范围内的连续记录")
            self.summary.setText(f"{self.category} · {stats['record_count']} 条记录 · 含估算 {stats['estimated_count']} 条 · 起止不全 {stats['unknown_count']} 条，未推算其时长。连续天数表示有记录，不表示达标。")
        else:
            for key in self.card_values:
                self.card_values[key].setText("—")
                self.card_values[key].setToolTip("")
                self.card_captions[key].setText("尚未读取")
            self.summary.setText("尚未读取统计范围；读取失败不会显示为零小时。")
        self.chart_title.setText({"daily": "时间热力图", "weekly": "每周记录时长", "cumulative": "已记录区间累计"}[self.mode])
        self.legend.setText({
            "daily": "浅 → 深：不足2小时 / 2–4 / 4–8 / 8–12 / 12小时以上；棕色角标含估算，斜纹为有记录但起止待补，空白为无记录。移入查看，点击查看活动及项目。",
            "weekly": "按周一至周日汇总区间并集，棕色部分为去重后的估算；范围首尾可能不足一周，无可计区间的周保留缺口。移入查看周统计。",
            "cumulative": "累计确定与估算区间并集，棕色标记含估算；虚线表示当天没有可计区间，累计保持不代表当天实际投入为零。",
        }[self.mode])
        self.chart.set_statistics(stats, self.mode)

    def select_date(self, key):
        self.day_selected.emit(key)
        self.accept()

    def choose_day(self, index):
        daily = self.statistics.get("daily", [])
        if 0 <= index < len(daily) and not daily[index].get("future"):
            self.select_date(daily[index]["date"])
