"""Quiet Windows widgets for recording; the controller owns all persistence/networking."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path

from PySide6.QtCore import Qt, QDate, QEvent, QSettings, QTimer, Signal, QPointF, QRectF
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QInputMethodEvent, QPainter, QColor, QPalette, QPen, QPixmap, QAction
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QSystemTrayIcon, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .timeline_widget import TimelineWidget, clock, duration, TAIPEI, palette_tokens
from .timeline_hover import TimelineHoverCard
from .date_picker import DualColumnDateEdit
from .desktop_windows import ensure_native_visible, suppress_native_transitions
from .heatmap_ui import HeatmapDialog
from .desktop_floating import FloatingPanel


# These are the approved handoff's light-theme tokens, not a new theme.
COLORS = palette_tokens(False)
STYLE = """
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 14px; color: $ink; }
QMainWindow, QDialog { background: $surface; }
QFrame#surface { background: $surface; border: 1px solid $line; border-radius: 16px; }
QLabel#title { font-size: 17px; font-weight: 500; }
QLabel#muted, QLabel#sync, QLabel#summary, QLabel#saveState, QLabel#detail { color: $muted; font-size: 12px; }
QFrame#selection { background: $canvas; border-top: 1px solid $line; border-bottom: 1px solid $line; }
QFrame#composer { background: $surface; }
QFrame#question { background: $canvas; border-top: 1px solid $line; }
QFrame#pending { border-top: 1px solid $line; }
QPlainTextEdit { background: $canvas; border: 1px solid $line; border-radius: 9px; padding: 9px 10px; selection-background-color: $work_fill; }
QPlainTextEdit:focus { border-color: $primary; }
QPushButton, QToolButton { background: $surface; border: 1px solid $line; border-radius: 7px; padding: 5px 8px; font-size: 12px; min-height: 20px; }
QPushButton:hover, QToolButton:hover { background: $canvas; }
QPushButton:focus, QToolButton:focus { border-color: $primary; }
QPushButton:checked { background: $primary; border-color: $primary; color: $primary_ink; }
QPushButton#record { background: $primary; color: $primary_ink; border: 0; border-radius: 8px; min-height: 22px; padding: 8px 13px; }
QPushButton#record:disabled { background: #9db6ae; color: white; }
QPushButton#retry { color: $primary; border-color: $primary; }
QPushButton#retry:disabled { color: $muted; border-color: $line; }
QPushButton#cluster { background: transparent; color: $primary; text-align: left; border: 0; padding: 4px 0; min-height: 18px; }
QToolButton#quiet { background: transparent; border: 0; color: $muted; padding: 2px 4px; }
QDateEdit, QComboBox { background: $surface; border: 1px solid $line; border-radius: 7px; padding: 5px 7px; font-size: 12px; min-height: 20px; }
QComboBox::drop-down, QDateEdit::drop-down { border: 0; width: 20px; }
QComboBox QAbstractItemView { background: $surface; selection-background-color: $work_fill; }
QScrollArea { background: $surface; border: 0; border-top: 1px solid $line; }
QListWidget, QTreeWidget { background: $surface; border: 0; font-size: 12px; }
QListWidget::item { padding: 6px 0; }
QListWidget::item:selected, QTreeWidget::item:selected { background: $work_fill; color: $ink; }
QScrollBar:vertical { background: $surface; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #a5aaaf; border-radius: 6px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: $surface; border: 1px solid $line; padding: 5px; }
QMenu::item:selected { background: $work_fill; }
"""
for _name, _value in sorted(COLORS.items(), key=lambda pair: -len(pair[0])):
    STYLE = STYLE.replace("$" + _name, _value)


def reported_duration(item):
    text = duration(item["reported_duration_seconds"])
    return "约" + text if item.get("estimated") and not text.startswith("约") else text


def load_offscreen_fonts():
    """Qt's offscreen plugin has no Windows font database; read installed OS fonts."""
    if QApplication.platformName() == "offscreen" and not QFontDatabase.families():
        for filename in ("msyh.ttc", "msyhbd.ttc", "consola.ttf"):
            path = Path("C:/Windows/Fonts") / filename
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))


def application_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(COLORS["primary"]))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(3, 3, 58, 58), 14, 14)
    p.setPen(QPen(QColor("#F5FAFD"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(21, 17), QPointF(21, 47))
    for y, width in ((20, 21), (32, 14), (44, 18)):
        p.drawLine(QPointF(30, y), QPointF(30 + width, y))
    p.end()
    return QIcon(pix)


class RecordEdit(QPlainTextEdit):
    submit_requested = Signal()
    composition_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.composing = False
        self._ime_enter_guard = False
        self._ime_guard_timer = QTimer(self)
        self._ime_guard_timer.setSingleShot(True)
        self._ime_guard_timer.setInterval(80)
        self._ime_guard_timer.timeout.connect(self._clear_ime_guard)
        self.setAcceptDrops(False)
        self._placeholder_example = "现在开始刷单词，AI 图片还在生成……"
        self.textChanged.connect(self._update_placeholder)
        self._update_placeholder()
        self.setAccessibleName("随手记内容，Enter记录，Ctrl加Enter换行，输入法选字不发送")
        self.setTabChangesFocus(True)

    def _update_placeholder(self):
        idle_empty = not self.hasFocus() and not self.composing and self.document().isEmpty()
        self.setPlaceholderText(self._placeholder_example if idle_empty else "")

    def focusInEvent(self, event):
        self.setPlaceholderText("")
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._update_placeholder()

    def _clear_ime_guard(self):
        if not self.composing:
            self._ime_enter_guard = False

    def inputMethodEvent(self, event: QInputMethodEvent):
        was_composing = self.composing
        self.composing = bool(event.preeditString())
        self._update_placeholder()
        if was_composing or self.composing or event.commitString():
            # Some IMEs deliver commit before the matching Return. Keep a
            # short same-key guard; a swallowed native key release cannot
            # leave the editor permanently waiting for an extra Enter.
            self._ime_enter_guard = True
            self._ime_guard_timer.start()
        super().inputMethodEvent(event)
        self.composition_changed.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.isAutoRepeat() or self.composing or self._ime_enter_guard:
                event.accept()
                return
            modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                self.insertPlainText("\n")
            elif modifiers == Qt.KeyboardModifier.NoModifier:
                self.submit_requested.emit()
            else:
                super().keyPressEvent(event)
                return
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self.composing:
            self._ime_guard_timer.stop()
            self._ime_enter_guard = False
        super().keyReleaseEvent(event)


class ActivityDialog(QDialog):
    correction_requested = Signal(dict)

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items
        self.setWindowTitle("活动详情")
        self.resize(510, 470)
        layout = QVBoxLayout(self)
        self.choose = QComboBox()
        for item in items:
            self.choose.addItem(item.get("name", "活动"))
        self.choose.setVisible(len(items) > 1)
        layout.addWidget(self.choose)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setObjectName("title")
        layout.addWidget(self.title)
        self.details = QTreeWidget()
        self.details.setHeaderLabels(["内容", "说明"])
        self.details.setColumnWidth(0, 135)
        self.details.setRootIsDecorated(True)
        layout.addWidget(self.details, 1)
        foot = QHBoxLayout()
        correct = QPushButton("补充 / 更正这项")
        correct.clicked.connect(self._correct)
        foot.addWidget(correct)
        foot.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        layout.addLayout(foot)
        self.choose.currentIndexChanged.connect(self._show_item)
        self._show_item(0)

    def _show_item(self, index):
        item = self.items[index]
        self.title.setText(item.get("name", "活动"))
        self.details.clear()
        def row(name, text):
            node = QTreeWidgetItem([name, str(text or "未知")])
            self.details.addTopLevelItem(node)
            return node
        row("实际起止", f"{clock(item.get('start_ms'), True)} → {clock(item.get('end_ms'), True)}")
        if item.get("report_ms") is not None:
            row("报告时刻", clock(item["report_ms"], True))
        estimate = item.get("display_estimate")
        if estimate:
            row("睡眠（报告估计）", f"{clock(estimate['start_ms'], True)} → {clock(estimate['end_ms'], True)}")
            row("估计说明", estimate.get("uncertainty") or "按睡前与醒来报告估计，非实测睡眠时长。")
            source = row("睡眠报告依据", "展开查看")
            for ref in estimate.get("source_refs", []):
                label = {"sleep_report": "睡前报告", "wake_report": "醒来报告"}.get(ref.get("role"), "报告")
                source.addChild(QTreeWidgetItem([label, clock(ref.get("report_ms"), True)]))
                if ref.get("id"):
                    source.child(source.childCount() - 1).addChild(QTreeWidgetItem(["来源", str(ref["id"])]))
        if item.get("original_start_ms") is not None or item.get("original_end_ms") is not None:
            if item.get("estimated"):
                row("原表估计边界", f"{clock(item.get('original_start_ms'), True)} → {clock(item.get('original_end_ms'), True)}（非精确实际起止）")
            else:
                row("正式表区间边界", f"{clock(item.get('original_start_ms'), True)} → {clock(item.get('original_end_ms'), True)}；记录区间不等同于有效人工")
        kind = {"recorded_window": "记录区间", "human_effort": "明确人工区间", "ai": "AI后台区间"}.get(item.get("duration_kind"), "有据区间")
        row(kind, duration(item.get("duration_seconds")))
        if item.get("reported_duration_seconds") is not None:
            row("单独报告的时长", reported_duration(item) + "；未直接加入人工并集")
        status = {"active": "尚未报告结束", "paused": "已暂停", "ended": "已结束", "completed": "已结束", "unknown": "未知", "uncertain": "有待补充", "recalled": "已撤回", "cancelled": "已取消"}.get(item.get("status"), item.get("status") or "未知")
        row("状态", status)
        row("项目 / 分类", " / ".join(str(x) for x in (item.get("project"), item.get("category")) if x) or "未指定")
        if item.get("result"):
            row("成果", item["result"])
        if item.get("uncertainty"):
            row("尚不明确", item["uncertainty"])
        subtasks = item.get("subtasks") or []
        if subtasks:
            parent = row("相关子任务", "展开查看；不重复加入人工汇总")
            for sub in subtasks:
                if isinstance(sub, str):
                    parent.addChild(QTreeWidgetItem([sub, ""]))
                else:
                    actor = {"human": "本人", "ai": "AI后台"}.get(sub.get("actor"), "")
                    parent.addChild(QTreeWidgetItem([str(sub.get("name", "子任务")), actor]))
        source = row("来源与修订", str(item.get("source") or "飞书正式时间表"))
        for record_id in item.get("record_ids") or []:
            source.addChild(QTreeWidgetItem(["正式记录", str(record_id)]))
        for ref in item.get("source_refs") or []:
            source.addChild(QTreeWidgetItem(["报告依据", str(ref)]))
        if item.get("revision") is not None:
            source.addChild(QTreeWidgetItem(["修订", str(item["revision"])]))
        self.details.expandToDepth(0)

    def _correct(self):
        self.correction_requested.emit(self.items[self.choose.currentIndex()])
        self.accept()


class DesktopWindow(QMainWindow):
    """Recording panel; background refreshes never activate or raise it."""
    quit_requested = Signal()

    def __init__(self, controller, *, settings=None, create_tray=True):
        super().__init__()
        self._normal_window_flags = self.windowFlags()
        load_offscreen_fonts()
        self.controller = controller
        self.settings = settings or QSettings("PersonalTimeLogger", "Desktop")
        self.floating_panel = None
        self._snapshot = {}
        self._expanded = False
        self._closing = False
        self._context = None
        self._questions = []
        self._deferred_questions = set()
        self._pending = []
        self._last_snapshot_date = None
        self._latest_scroll_requested = True
        self._draft_loading = True
        self._sync_busy = False
        self._overview_mode = True
        self.setWindowTitle("个人时间记录 · 随手记")
        self.setWindowIcon(application_icon())
        self.setMinimumSize(380, 265)
        self.resize(460, 300)
        light_palette = QPalette(self.palette())
        for role, token in ((QPalette.ColorRole.Window, "surface"),
                            (QPalette.ColorRole.Base, "surface"),
                            (QPalette.ColorRole.AlternateBase, "canvas"),
                            (QPalette.ColorRole.WindowText, "ink"),
                            (QPalette.ColorRole.Text, "ink"),
                            (QPalette.ColorRole.Button, "surface"),
                            (QPalette.ColorRole.ButtonText, "ink"),
                            (QPalette.ColorRole.Highlight, "work_fill"),
                            (QPalette.ColorRole.HighlightedText, "ink")):
            light_palette.setColor(role, QColor(COLORS[token]))
        self.setPalette(light_palette)
        self.setStyleSheet(STYLE)
        self._build()
        self.tray = None
        if create_tray:
            self._build_tray()
        self._connect_controller()
        try:
            self.editor.setPlainText(controller.get_draft() or "")
        except Exception:
            self.save_state.setText("草稿读取受阻；已保留的内容不会被清空")
        self._draft_loading = False
        geometry = self.settings.value("geometry")
        migrated = self.settings.value("layout_version", "") == "reference-1.3"
        if geometry:
            self.restoreGeometry(geometry)
        expanded = self.settings.value("expanded", False, type=bool)
        if not migrated:
            # Apply the approved narrow layout once, retaining the position.
            self.resize(460, 740 if expanded else 300)
            self.settings.setValue("layout_version", "reference-1.3")
        self.set_expanded(expanded, remember=False)
        self._keep_on_screen()
        self.pin.setChecked(self.settings.value("topmost", False, type=bool))
        self._update_submit_enabled()
        if create_tray and os.name == "nt":
            self.floating_panel = FloatingPanel(self)
            self.floating_action.setChecked(self.floating_panel.enabled)
            self.keep_open_action.setEnabled(self.floating_panel.enabled)
            self._apply_topmost()
            self.quit_requested.connect(self.floating_panel.dispose)
            QApplication.instance().aboutToQuit.connect(self.floating_panel.dispose)
            QTimer.singleShot(0, self.floating_panel.start)

    def _build(self):
        central = QFrame()
        central.setObjectName("surface")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(18, 14, 18, 8)
        self.title = QLabel("时间记录")
        self.title.setObjectName("title")
        header.addWidget(self.title)
        header.addStretch()
        self.connection_label = QLabel("正在读取")
        self.connection_label.setObjectName("muted")
        header.addWidget(self.connection_label)
        self.heatmap_button = QPushButton("热力图")
        self.heatmap_button.clicked.connect(self.show_heatmap)
        header.addWidget(self.heatmap_button)
        self.expand_button = QToolButton()
        self.expand_button.setObjectName("quiet")
        self.expand_button.clicked.connect(lambda: self.set_expanded(not self._expanded))
        header.addWidget(self.expand_button)
        menu_button = QToolButton()
        menu_button.setObjectName("quiet")
        menu_button.setText("···")
        menu_button.setAccessibleName("窗口选项")
        menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(menu_button)
        menu.addAction("刷新历史", self.refresh)
        menu.addAction("月度热力图", self.show_heatmap)
        self.retry_action = menu.addAction("立即重试同步", self._retry_sync)
        menu.addAction("查看昨日", lambda: self._choose_relative_date(-1))
        self.pin = QCheckBox("置顶", self)
        self.pin.hide()
        self.pin.toggled.connect(self._pin_changed)
        pin_action = menu.addAction("置顶")
        pin_action.setCheckable(True)
        pin_action.toggled.connect(self.pin.setChecked)
        self.pin.toggled.connect(pin_action.setChecked)
        menu.addSeparator()
        self.floating_action = QAction("悬浮入口", self)
        self.floating_action.setCheckable(True)
        self.floating_action.setVisible(os.name == "nt")
        self.floating_action.toggled.connect(self._floating_changed)
        self.keep_open_action = QAction("保持展开", self)
        self.keep_open_action.setCheckable(True)
        self.keep_open_action.setVisible(os.name == "nt")
        self.keep_open_action.setEnabled(False)
        self.keep_open_action.toggled.connect(self._keep_open_changed)
        menu.addAction(self.floating_action)
        menu.addAction(self.keep_open_action)
        menu.addAction("收起小窗", self.hide_to_tray)
        menu.addAction("退出", self.request_quit)
        menu_button.setMenu(menu)
        header.addWidget(menu_button)
        outer.addLayout(header)
        self.compact_summary = QLabel("正在读取最近记录…")
        self.compact_summary.setObjectName("summary")
        self.compact_summary.setContentsMargins(18, 0, 18, 8)
        self.compact_summary.setWordWrap(True)
        self.compact_summary.setMaximumHeight(58)
        self.compact_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.compact_summary)
        self.question_prompt = QPushButton("有待确认信息 · 查看")
        self.question_prompt.setObjectName("cluster")
        self.question_prompt.setContentsMargins(18, 0, 18, 0)
        self.question_prompt.clicked.connect(self._show_question_in_history)
        self.question_prompt.hide()
        outer.addWidget(self.question_prompt)

        self.history = QWidget()
        hist = QVBoxLayout(self.history)
        hist.setContentsMargins(0, 0, 0, 0)
        hist.setSpacing(0)
        nav = QHBoxLayout()
        nav.setContentsMargins(18, 2, 18, 8)
        nav.setSpacing(4)
        for text, offset in (("今日", 0), ("昨日", -1)):
            button = QPushButton(text)
            button.setFixedWidth(44)
            button.clicked.connect(lambda checked=False, n=offset: self._choose_relative_date(n))
            nav.addWidget(button)
        self.date_edit = DualColumnDateEdit()
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setFixedWidth(113)
        today = datetime.now(TAIPEI).date()
        self.date_edit.setDate(QDate(today.year, today.month, today.day))
        self.date_edit.dateChanged.connect(self._date_changed)
        nav.addWidget(self.date_edit)
        nav.addStretch(1)
        self.overview_button = QPushButton("概览")
        self.zoom_button = QPushButton("放大")
        for button in (self.overview_button, self.zoom_button):
            button.setCheckable(True)
            button.setFixedWidth(44)
            nav.addWidget(button)
        self.overview_button.setChecked(True)
        self.overview_button.clicked.connect(lambda: self.set_time_zoom(3))
        self.zoom_button.clicked.connect(lambda: self.set_time_zoom(8))
        hist.addLayout(nav)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("summary")
        self.summary_label.setContentsMargins(18, 0, 18, 6)
        self.summary_label.setWordWrap(True)
        self.summary_label.setMaximumHeight(55)
        self.summary_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hist.addWidget(self.summary_label)
        self.sync_label = QLabel("尚未取得正式历史")
        self.sync_label.setObjectName("sync")
        self.sync_label.setContentsMargins(18, 0, 18, 5)
        self.sync_label.setWordWrap(True)
        self.sync_label.setMaximumHeight(35)
        self.sync_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hist.addWidget(self.sync_label)
        self.cluster_button = QPushButton("")
        self.cluster_button.setObjectName("cluster")
        self.cluster_button.clicked.connect(self._zoom_cluster)
        cluster_row = QHBoxLayout()
        cluster_row.setContentsMargins(18, 0, 18, 7)
        cluster_row.addWidget(self.cluster_button)
        hist.addLayout(cluster_row)
        self.cluster_button.hide()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(70)
        axis_container = QWidget()
        axis_layout = QVBoxLayout(axis_container)
        axis_layout.setContentsMargins(18, 0, 14, 0)
        axis_layout.setSpacing(0)
        self.timeline = TimelineWidget()
        self.timeline_hover = TimelineHoverCard(self)
        self.timeline.tag_hovered.connect(self.timeline_hover.show_records)
        self.timeline.tag_left.connect(self.timeline_hover.schedule_hide)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _: self.timeline.update())
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _: self.timeline_hover.dismiss())
        self.overview_timer = QTimer(self)
        self.overview_timer.setSingleShot(True)
        self.overview_timer.timeout.connect(self._fit_overview)
        self.overview_timer.timeout.connect(self._queue_latest_scroll)
        self.latest_scroll_timer = QTimer(self)
        self.latest_scroll_timer.setSingleShot(True)
        self.latest_scroll_timer.timeout.connect(self._scroll_to_latest)
        self.scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self._queue_latest_scroll())
        self.scroll.viewport().installEventFilter(self)
        axis_layout.addWidget(self.timeline)
        self.unlocated_title = QLabel("未知起止 / 仅有总时长")
        self.unlocated_title.setObjectName("muted")
        self.unlocated_title.setContentsMargins(0, 12, 0, 3)
        axis_layout.addWidget(self.unlocated_title)
        self.unlocated = QListWidget()
        self.unlocated.setWordWrap(True)
        self.unlocated.itemActivated.connect(lambda item: self.show_activities([item.data(Qt.ItemDataRole.UserRole)]))
        self.unlocated.itemClicked.connect(lambda item: self.show_activities([item.data(Qt.ItemDataRole.UserRole)]))
        axis_layout.addWidget(self.unlocated)
        self.pending_frame = QFrame()
        self.pending_frame.setObjectName("pending")
        pending_layout = QVBoxLayout(self.pending_frame)
        pending_layout.setContentsMargins(0, 12, 0, 8)
        pending_layout.setSpacing(5)
        pending_header = QHBoxLayout()
        self.pending_toggle = QToolButton()
        self.pending_toggle.setObjectName("quiet")
        self.pending_toggle.setText("待整理")
        self.pending_toggle.clicked.connect(lambda: self.pending_list.setVisible(not self.pending_list.isVisible()))
        pending_header.addWidget(self.pending_toggle)
        pending_header.addStretch()
        self.pending_count = QLabel("0 条 · 不计入合计")
        self.pending_count.setObjectName("muted")
        pending_header.addWidget(self.pending_count)
        pending_layout.addLayout(pending_header)
        self.pending_list = QListWidget()
        self.pending_list.setWordWrap(True)
        self.pending_list.setMinimumHeight(45)
        pending_layout.addWidget(self.pending_list)
        self.pending_frame.hide()
        axis_layout.addWidget(self.pending_frame)
        self.axis_note = QLabel("斜纹表示缺少记录；记录区间不等同于有效人工，AI单列。")
        self.axis_note.setObjectName("muted")
        self.axis_note.setWordWrap(True)
        self.axis_note.setContentsMargins(0, 6, 0, 10)
        axis_layout.addWidget(self.axis_note)

        self.question_frame = QFrame()
        self.question_frame.setObjectName("question")
        qlayout = QVBoxLayout(self.question_frame)
        qlayout.setContentsMargins(0, 10, 0, 10)
        self.question_label = QLabel()
        self.question_label.setWordWrap(True)
        self.question_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        qlayout.addWidget(self.question_label)
        question_actions = QHBoxLayout()
        for text, callback in (("回答", self._answer_question), ("不确定", self._skip_question), ("稍后", self._later_question)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            question_actions.addWidget(button)
        question_actions.addStretch()
        qlayout.addLayout(question_actions)
        self.question_frame.hide()
        axis_layout.addWidget(self.question_frame)
        self.scroll.setWidget(axis_container)
        hist.addWidget(self.scroll, 1)

        self.selection_frame = QFrame()
        self.selection_frame.setObjectName("selection")
        selection = QVBoxLayout(self.selection_frame)
        selection.setContentsMargins(18, 10, 18, 11)
        selection.setSpacing(6)
        node_row = QHBoxLayout()
        node_row.setSpacing(7)
        label = QLabel("查看节点")
        label.setObjectName("muted")
        node_row.addWidget(label)
        self.node_select = QComboBox()
        self.node_select.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.node_select.setMinimumContentsLength(2)
        self.node_select.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.node_select.setAccessibleName("选择时间节点；密集和重合节点各自可查看")
        self.node_select.currentIndexChanged.connect(self._node_changed)
        node_row.addWidget(self.node_select, 1)
        details = QToolButton()
        details.setText("详情")
        details.clicked.connect(self._show_selected_activity)
        node_row.addWidget(details)
        selection.addLayout(node_row)
        self.detail_label = QLabel("选择节点查看依据；起止未知不补造区间。")
        self.detail_label.setObjectName("detail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.detail_label.setMinimumHeight(36)
        self.detail_label.setMaximumHeight(42)
        selection.addWidget(self.detail_label)
        hist.addWidget(self.selection_frame)
        outer.addWidget(self.history, 1)

        self.composer = QFrame()
        self.composer.setObjectName("composer")
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(18, 12, 18, 14)
        composer_layout.setSpacing(7)
        self.context_frame = QWidget()
        context_layout = QHBoxLayout(self.context_frame)
        context_layout.setContentsMargins(0, 0, 0, 0)
        self.context_label = QLabel()
        self.context_label.setWordWrap(True)
        self.context_label.setMaximumHeight(36)
        self.context_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        context_layout.addWidget(self.context_label, 1)
        clear_context = QToolButton()
        clear_context.setText("取消关联")
        clear_context.clicked.connect(self.clear_context)
        context_layout.addWidget(clear_context)
        self.context_frame.hide()
        composer_layout.addWidget(self.context_frame)
        input_heading = QHBoxLayout()
        input_heading.addWidget(QLabel("随手记"))
        input_heading.addStretch()
        hint = QLabel("Enter 记录 · Ctrl+Enter 换行")
        hint.setObjectName("muted")
        input_heading.addWidget(hint)
        composer_layout.addLayout(input_heading)
        self.editor = RecordEdit()
        self.editor.setFixedHeight(74)
        self.editor.textChanged.connect(self._draft_changed)
        self.editor.composition_changed.connect(self._update_submit_enabled)
        self.editor.submit_requested.connect(self.submit)
        composer_layout.addWidget(self.editor)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.save_state = QLabel("草稿未提交")
        self.save_state.setObjectName("saveState")
        self.save_state.setWordWrap(True)
        self.save_state.setMaximumHeight(54)
        self.save_state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.save_state, 1)
        self.retry_button = QPushButton("立即重试")
        self.retry_button.setObjectName("retry")
        self.retry_button.setFixedWidth(84)
        self.retry_button.setAccessibleName("立即重试同步已保存记录")
        self.retry_button.setToolTip("立即重试已保存记录的同步")
        self.retry_button.clicked.connect(self._retry_sync)
        footer.addWidget(self.retry_button)
        self.record_button = QPushButton("记录")
        self.record_button.setObjectName("record")
        self.record_button.clicked.connect(self.submit)
        footer.addWidget(self.record_button)
        composer_layout.addLayout(footer)
        outer.addWidget(self.composer)
        self.draft_timer = QTimer(self)
        self.draft_timer.setSingleShot(True)
        self.draft_timer.setInterval(180)
        self.draft_timer.timeout.connect(self._save_draft)

    def set_time_zoom(self, value, focus_ms=None):
        self.timeline_hover.dismiss()
        self._overview_mode = value == 3
        self.overview_button.setChecked(self._overview_mode)
        self.zoom_button.setChecked(not self._overview_mode)
        if self._overview_mode:
            self._fit_overview()
            self.scroll.verticalScrollBar().setValue(0)
            return
        old = self.timeline.scale
        scroll = self.scroll.verticalScrollBar()
        center = scroll.value() - self.timeline.y() + self.scroll.viewport().height() / 2
        anchor = focus_ms if focus_ms is not None else old.start_ms + (center - old.top) / old.pixels_per_minute * 60000
        anchor = max(old.start_ms, min(old.end_ms, anchor))
        self.timeline.set_zoom(value)
        QTimer.singleShot(0, lambda: scroll.setValue(int(max(0, self.timeline.y() + self.timeline.scale.y(anchor) - (50 if focus_ms is not None else self.scroll.viewport().height() / 2)))))
        self._update_cluster()

    def _fit_overview(self):
        if not self._overview_mode or not self._snapshot:
            return
        scale = self.timeline.scale
        minutes = max(1, (scale.end_ms - scale.start_ms) / 60000)
        # One uniform scale for the entire visible range. The composer and
        # history controls retain their sizes; dense events use zoom/details.
        available = max(30, self.scroll.viewport().height() - scale.top - 24 - 38)
        value = min(3.0, available / minutes)
        if abs(value - scale.pixels_per_minute) > .000001:
            self.timeline.set_zoom(value)
        self._update_cluster()

    def eventFilter(self, watched, event):
        if hasattr(self, "scroll") and watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.overview_timer.start(0)
        return super().eventFilter(watched, event)

    def _request_latest_scroll(self):
        self._latest_scroll_requested = True
        self.overview_timer.start(0)

    def _queue_latest_scroll(self):
        if (self._latest_scroll_requested and self._snapshot and self.isVisible()
                and self._expanded and not self.overview_timer.isActive()):
            self.latest_scroll_timer.start(0)

    def _scroll_to_latest(self):
        if not self._latest_scroll_requested or not self.isVisible() or not self._expanded:
            return
        # Layout requests and the overview fit may both change the final range.
        # Settle them before consuming this one-shot opening/date-change intent.
        for widget in (self.centralWidget(), self.history, self.scroll.widget()):
            if widget.layout():
                widget.layout().activate()
        if self.overview_timer.isActive():
            return
        scroll = self.scroll.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        self._latest_scroll_requested = False

    def _update_cluster(self):
        dense = next((g for g in self.timeline.groups if len(g) > 1), [])
        self.cluster_button.setVisible(bool(dense))
        if dense:
            times = list(dict.fromkeys(clock(a["timestamp"]) for a in dense))[:3]
            self.cluster_button.setText(f"{len(dense)} 个密集节点：" + "、".join(times) + " · 放大")
            self.cluster_button.setToolTip("仅统一缩放，不改变节点时间或活动时长")
        self._dense_group = dense

    def _zoom_cluster(self):
        if getattr(self, "_dense_group", None):
            self.set_time_zoom(8, self._dense_group[0]["timestamp"])

    def _node_changed(self, index, *, ensure_visible=True):
        if index < 0:
            return
        key = self.node_select.itemData(index)
        event = next((a for a in self.timeline.events() if a["key"] == key), None)
        if not event:
            return
        self.timeline.select_event(key)
        item = event["item"]
        text = event["title"] + "。"
        if event["kind"] == "report":
            text += "报告时刻，不代表实际开始；起止未知。"
        elif event["kind"] == "continuation":
            text += "原始开始 " + clock(item.get("start_ms"), True) + "。"
        elif event["kind"].startswith("estimated_"):
            text += "依据睡前与醒来报告估计；详情可查看依据。"
        elif item.get("start_ms") is not None and item.get("end_ms") is None:
            text += "尚未报告结束。"
        if item.get("duration_kind") == "recorded_window":
            text += "记录区间不等于有效人工。"
        elif any(bar["item"] is item and bar["kind"] == "window" for bar in self.timeline.bars):
            text += "仅明确活动起止窗口，连续人工投入未知。"
        if item.get("uncertainty"):
            text += item["uncertainty"]
        self.detail_label.setText(text)
        self.detail_label.setToolTip(text)
        if not ensure_visible:
            return
        self._latest_scroll_requested = False
        y = self.timeline.y() + event["y"]
        scroll = self.scroll.verticalScrollBar()
        if y < scroll.value() + 24 or y > scroll.value() + self.scroll.viewport().height() - 24:
            scroll.setValue(int(max(0, y - self.scroll.viewport().height() / 2)))

    def _show_selected_activity(self):
        key = self.node_select.currentData()
        event = next((a for a in self.timeline.events() if a["key"] == key), None)
        if event:
            self.show_activities([event["item"]])

    def _show_question_in_history(self):
        self.set_expanded(True)
        self._latest_scroll_requested = False
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.question_frame))

    def set_hotkey(self, label):
        """Display the shortcut only after the native host successfully registers it."""
        self.setWindowTitle("个人时间记录 · 随手记" + (f" · {label}" if label else ""))
        self.expand_button.setToolTip(f"全局呼出/收起：{label}" if label else "也可从托盘或桌面入口打开")
        if self.tray:
            self.tray.setToolTip(f"个人时间记录 · {label}" if label else "个人时间记录 · 点击随手记")

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("个人时间记录 · 点击随手记")
        menu = QMenu()
        show = menu.addAction("展开随手记")
        show.triggered.connect(self.show_and_focus)
        menu.addAction("展开今日时间线", self._show_today)
        menu.addSeparator()
        menu.addAction(self.floating_action)
        menu.addAction(self.keep_open_action)
        menu.addSeparator()
        menu.addAction("退出（下次打开继续上传）", self.request_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.toggle_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self.tray.show()

    def _connect_controller(self):
        self.controller.snapshot.connect(self.receive_snapshot)
        self.controller.state.connect(self._receive_state)
        self.controller.pending.connect(self.receive_pending)
        self.controller.questions.connect(self.receive_questions)
        self.controller.sync_busy.connect(self._set_sync_busy)

    def show_heatmap(self):
        if not hasattr(self.controller, "request_heatmap"):
            return
        dialog = getattr(self, "_heatmap_dialog", None)
        if dialog is None:
            dialog = HeatmapDialog(self.selected_date(), self)
            self._heatmap_dialog = dialog
            dialog.month_requested.connect(lambda month, refresh: self.controller.request_heatmap(month, refresh=refresh, scope=dialog.scope))
            dialog.day_selected.connect(self._heatmap_day_selected)
            self.controller.heatmap_snapshot.connect(dialog.receive_snapshot)
            self.controller.heatmap_state.connect(dialog.set_status)
            dialog.finished.connect(lambda _: self.controller.set_heatmap_visible(False))
        if self.floating_panel is not None:
            self.floating_panel.attach_heatmap(dialog)
            self.floating_panel.heatmap_transition.cancel()
        dialog.show()
        dialog.raise_()
        self.controller.request_heatmap(dialog.month_key(), scope=dialog.scope)

    def _heatmap_day_selected(self, day):
        chosen = QDate.fromString(day, "yyyy-MM-dd")
        self.set_expanded(True)
        if self.date_edit.date() == chosen:
            self.refresh()
        else:
            self.date_edit.setDate(chosen)

    def _set_sync_busy(self, busy):
        self._sync_busy = bool(busy)
        self.retry_button.setText("同步中…" if busy else "立即重试")
        self.retry_button.setEnabled(not busy)
        self.retry_action.setEnabled(not busy)

    def _retry_sync(self):
        if self._sync_busy:
            return
        self._set_sync_busy(True)
        try:
            self.controller.retry_sync()
        except Exception:
            self._set_sync_busy(False)
            self.save_state.setText("同步暂时受阻，原文已保留；可再次重试")

    def _receive_state(self, text):
        self.save_state.setText(str(text))

    def _draft_changed(self):
        self._update_submit_enabled()
        if not self._draft_loading:
            self.save_state.setText("正在保留草稿…")
            self.draft_timer.start()

    def _update_submit_enabled(self):
        self.record_button.setEnabled(bool(self.editor.toPlainText().strip()) and not self.editor.composing)

    def _save_draft(self):
        self.draft_timer.stop()
        try:
            result = self.controller.set_draft(self.editor.toPlainText())
            if result is False:
                raise OSError("draft_not_persisted")
            if self.editor.toPlainText():
                self.save_state.setText("草稿已保留 · 尚未提交")
        except Exception:
            self.save_state.setText("草稿暂未存下，请保留窗口中的内容")
            return False
        return True

    def submit(self):
        if self.editor.composing:
            return
        text = self.editor.toPlainText().strip()
        if not text:
            return
        context = self._context or {}
        kwargs = {}
        if context.get("activity_id"):
            kwargs["activity_id"] = context["activity_id"]
        elif context.get("record_id"):
            kwargs.update(record_id=context["record_id"], expected_fingerprint=context.get("expected_fingerprint"))
        if context.get("question_id"):
            kwargs.update(question_id=context["question_id"], answer_action="answer")
        try:
            result = self.controller.submit(text, **kwargs)
            if result is False:
                raise OSError("submission_not_persisted")
        except Exception:
            self.save_state.setText("尚未存下，内容仍在输入框；可稍后重试")
            return
        self.draft_timer.stop()
        self._draft_loading = True
        self.editor.clear()
        self._draft_loading = False
        try:
            self.controller.set_draft("")
        except Exception:
            # A retained old draft is safer than clearing text that was not durable.
            pass
        self.clear_context()
        self._update_submit_enabled()
        self.editor.setFocus()

    def receive_snapshot(self, snapshot):
        if snapshot.get("date") and snapshot["date"] != self.selected_date():
            return
        self._snapshot = snapshot
        self.timeline_hover.dismiss()
        self.timeline.show()
        selected = self.node_select.currentData()
        self.timeline.set_snapshot(snapshot)
        self.node_select.blockSignals(True)
        self.node_select.clear()
        for event in self.timeline.events():
            self.node_select.addItem(event["title"], event["key"])
        index = self.node_select.findData(selected)
        self.node_select.setCurrentIndex(index if index >= 0 else 0)
        self.node_select.blockSignals(False)
        self._node_changed(self.node_select.currentIndex(), ensure_visible=False)
        if not self.node_select.count():
            self.detail_label.setText("该日无可定位节点；未知与估算记录保留在历史中。")
            self.detail_label.setToolTip("")
        self._update_cluster()
        summary = snapshot.get("summary", {})
        work = summary.get("work_study_seconds")
        human = summary.get("human_seconds")
        ai = summary.get("ai_seconds")
        parts = [f"工作 / 学习 {duration(work)}", f"生活 / 其他 {duration(summary.get('other_seconds'))}", f"AI {duration(ai)}（单列）"]
        extra = [f"已确认人工 {duration(human)}"]
        if summary.get("recorded_seconds") is not None:
            extra.append(f"已记录区间 {duration(summary['recorded_seconds'])}")
        if summary.get("scalar_seconds") is not None:
            extra.append(f"估算/标量 {duration(summary['scalar_seconds'])}（单列，不加人工）")
        self.summary_label.setText(" · ".join(parts) + ("\n" + " · ".join(extra) if extra else ""))
        compact = f"已确认人工 {duration(human)}"
        items = snapshot.get("activities", snapshot.get("items", []))
        if items:
            latest = max(items, key=lambda a: a.get("report_ms") or a.get("start_ms") or 0)
            compact += f" · 最近：{latest.get('name', '活动')}"
        elif human is None:
            compact = "当天无可用记录；不能据此判定投入为零"
        server_now = snapshot.get("server_now_ms")
        today = datetime.fromtimestamp(server_now / 1000, TAIPEI).date().isoformat() if server_now else datetime.now(TAIPEI).date().isoformat()
        if snapshot.get("date") and snapshot["date"] != today:
            compact = f"回看 {snapshot['date']} · " + compact
        self.compact_summary.setText(compact)
        cache = snapshot.get("cache", {})
        as_of = snapshot.get("as_of_ms") or cache.get("as_of_ms")
        state = cache.get("state", "fresh")
        prefix = "缓存" if state not in ("fresh", "live", "updated") or cache.get("stale") or cache.get("local_cached") else "正式历史"
        self.sync_label.setText(f"{prefix}截至 {clock(as_of, True)} · {clock(self.timeline.scale.start_ms)}—{clock(self.timeline.scale.end_ms)}" + (" · 同步受阻" if cache.get("refresh_error") else ""))
        self.connection_label.setText("已连接记录" if prefix == "正式历史" else "查看缓存")
        self.unlocated.clear()
        unlocated = [a for a in items if a.get("start_ms") is None and a.get("end_ms") is None and a.get("report_ms") is None]
        for item in unlocated:
            extra = "起止未知"
            if item.get("reported_duration_seconds") is not None:
                extra += " · " + reported_duration(item) + "（单列）"
            row = QListWidgetItem(f"{item.get('name', '活动')} · {extra}")
            row.setData(Qt.ItemDataRole.UserRole, item)
            self.unlocated.addItem(row)
        self.unlocated_title.setVisible(bool(unlocated))
        self.unlocated.setVisible(bool(unlocated))
        self.unlocated.setFixedHeight(min(180, 42 * len(unlocated) + 8))
        if summary.get("recorded_seconds") is not None:
            self.axis_note.setText(f"已记录区间 {duration(summary['recorded_seconds'])}；记录区间不等同于有效人工，AI单列。斜纹表示缺少连续区间记录。")
        self.timeline.setAccessibleDescription(f"{snapshot.get('date', '')}；数据截至{clock(as_of, True)}；所有位置按统一时间比例。")
        if self._last_snapshot_date != snapshot.get("date"):
            self._last_snapshot_date = snapshot.get("date")
            self._latest_scroll_requested = True
        self.overview_timer.start(0)

    def receive_pending(self, pending):
        self._pending = list(pending)
        self.pending_list.clear()
        for item in pending:
            text = item.get("text", item.get("content", ""))
            report_ms = item.get("report_ms", item.get("reported_at_ms", item.get("created_at_ms")))
            stages = {"local_saved": "已存本机，待上传", "local_needs_review": "本机原文已保留，需要重新选择更正对象", "server_saved": "服务器已保存，待整理", "organized": "已整理，待飞书核验", "archived": "已归档并核验", "needs_review": "需要补充或核对"}
            stage = item.get("stage", item.get("state", item.get("status", "server_saved")))
            state = stages.get(stage, stage)
            if stage == "organized" and (item.get("base_required") is False or (item.get("response") or {}).get("base_required") is False):
                state = "已整理 · AI后台单列"
            if item.get("error") and stage != "local_needs_review":
                state += " · 暂时同步受阻"
            if item.get("chat_mirror_status"):
                state += " · " + item["chat_mirror_status"]
            self.pending_list.addItem(f"{clock(report_ms, True)} · {state}\n{text}")
        self.pending_toggle.setText("待整理")
        self.pending_count.setText(f"{len(pending)} 条 · 不计入合计")
        self.pending_frame.setVisible(bool(pending))
        self.pending_list.setFixedHeight(min(260, max(45, len(pending) * 66)))

    def receive_questions(self, questions):
        self._questions = [q for q in questions if q.get("status", "pending") in ("pending", "asked") and q.get("question_id", q.get("id")) not in self._deferred_questions]
        self._show_question()

    def _show_question(self):
        self.question_frame.setVisible(bool(self._questions))
        self.question_prompt.setVisible(bool(self._questions))
        if self._questions:
            q = self._questions[0]
            body = q.get("body", {})
            text = q.get("text") or q.get("question") or (body.get("text") if isinstance(body, dict) else body) or q.get("prompt") or "有一项时间信息需要补充"
            self.question_label.setText(str(text))

    def _question_id(self):
        q = self._questions[0]
        return q.get("question_id", q.get("id"))

    def _answer_question(self):
        if self._questions:
            self._context = {"question_id": self._question_id()}
            self.context_label.setText("正在回答：" + self.question_label.text())
            self.context_frame.show()
            self.editor.setFocus()

    def _skip_question(self):
        if not self._questions:
            return
        qid = self._question_id()
        try:
            if self.controller.submit("", question_id=qid, answer_action="skip") is False:
                raise OSError("skip_not_saved")
        except Exception:
            self.save_state.setText("答复尚未保存，可稍后继续记录")
            return
        self._deferred_questions.add(qid)
        self._questions.pop(0)
        self._show_question()

    def _later_question(self):
        if self._questions:
            self._deferred_questions.add(self._question_id())
            self._questions.pop(0)
            self._show_question()

    def clear_context(self):
        self._context = None
        self.context_frame.hide()

    def _correct_activity(self, item):
        if item.get("activity_id"):
            self._context = {"activity_id": item["activity_id"]}
        elif item.get("record_id") or item.get("record_ids"):
            rid = item.get("record_id") or item["record_ids"][0]
            self._context = {"record_id": rid, "expected_fingerprint": item.get("record_fingerprint", item.get("expected_fingerprint"))}
        else:
            self.save_state.setText("暂时无法关联这项；可在原文中写明日期与活动名称")
            return
        self.context_label.setText("补充 / 更正：" + item.get("name", "活动"))
        self.context_frame.show()
        self.editor.setFocus()

    def show_activities(self, items):
        if items:
            dialog = ActivityDialog(items, self)
            dialog.correction_requested.connect(self._correct_activity)
            dialog.exec()

    def selected_date(self):
        return self.date_edit.date().toString("yyyy-MM-dd")

    def _choose_relative_date(self, offset):
        day = datetime.now(TAIPEI).date() + timedelta(days=offset)
        chosen = QDate(day.year, day.month, day.day)
        if self.date_edit.date() == chosen:
            self.refresh()
        else:
            self.date_edit.setDate(chosen)

    def _date_changed(self, day):
        self.timeline_hover.dismiss()
        self._latest_scroll_requested = True
        self._snapshot = {}
        self._last_snapshot_date = None
        self.summary_label.setText("正在读取该日正式历史…")
        self.sync_label.setText("日期只影响回看；普通新记录仍按本次提交时刻保存")
        self.timeline.hide()
        self.timeline.set_snapshot({})
        self.node_select.clear()
        self.timeline.select_event(None)
        self.detail_label.setText("正在读取该日节点…")
        self.detail_label.setToolTip("")
        self.unlocated.hide()
        self.unlocated_title.hide()
        self.axis_note.setText("正在读取该日历史；暂不显示其他日期的数据。")
        try:
            self.controller.load_cached(day.toString("yyyy-MM-dd"))
            self.controller.refresh(day.toString("yyyy-MM-dd"))
        except Exception:
            self.sync_label.setText("该日历史暂时无法读取；原有缓存保留")

    def refresh(self):
        self.controller.refresh(self.selected_date())

    def set_expanded(self, expanded, *, remember=True):
        changed = expanded != self._expanded
        self._expanded = expanded
        self.history.setVisible(expanded)
        self.compact_summary.setVisible(not expanded)
        self.expand_button.setText("收起" if expanded else "展开时间线")
        self.setMinimumSize(380, 540 if expanded else 265)
        if changed:
            available = QApplication.primaryScreen().availableGeometry().height() if QApplication.primaryScreen() else 900
            self.resize(self.width(), min(740, available - 48) if expanded else 300)
        if remember:
            self.settings.setValue("expanded", expanded)
        if expanded:
            if changed:
                self._request_latest_scroll()
            self.refresh()

    def _show_today(self):
        self._choose_relative_date(0)
        self.set_expanded(True)
        self.show_and_focus()

    def _pin_changed(self, pinned):
        self.settings.setValue("topmost", pinned)
        self._apply_topmost()

    def _floating_changed(self, enabled):
        if self.floating_panel is None:
            return
        self.floating_panel.set_enabled(enabled)
        self.keep_open_action.setEnabled(enabled)
        self._apply_topmost()

    def _keep_open_changed(self, keep_open):
        if self.floating_panel is not None:
            self.floating_panel.set_keep_open(keep_open)

    def _apply_topmost(self):
        floating = self.floating_panel is not None and self.floating_panel.enabled
        flags = ((Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                  | Qt.WindowType.WindowStaysOnTopHint) if floating else self._normal_window_flags)
        if self.pin.isChecked():
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if self.windowFlags() == flags:
            return
        was_visible, was_active = self.isVisible(), self.isActiveWindow()
        self.setWindowFlags(flags)
        suppress_native_transitions(int(self.winId()), floating)
        if was_visible:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.show()
            ensure_native_visible(int(self.winId()), activate=False)
            if was_active:
                self.activateWindow()

    def _keep_on_screen(self):
        if not any(screen.availableGeometry().intersects(self.frameGeometry()) for screen in QApplication.screens()):
            screen = QApplication.primaryScreen()
            if screen:
                self.move(screen.availableGeometry().center() - self.rect().center())

    def _save_preferences(self):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("expanded", self._expanded)
        self.settings.sync()

    def show_and_focus(self):
        if self.floating_panel is not None and self.floating_panel.enabled:
            self.floating_panel.reveal(focus=True)
        else:
            self._show_panel(focus=True)

    def show_without_focus(self):
        """Hover reveals the panel without taking another app's input focus."""
        if self.floating_panel is not None and self.floating_panel.enabled:
            self.floating_panel.reveal(focus=False)
        else:
            self._show_panel(focus=False)

    def _show_panel(self, *, focus):
        self._request_latest_scroll()
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not focus)
        self.showNormal()
        ensure_native_visible(int(self.winId()), activate=focus)
        if focus:
            self.raise_()
            self.activateWindow()
            self.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.refresh()

    def toggle_window(self):
        if self.isVisible() and self.isActiveWindow():
            self.hide_to_tray()
        else:
            self.show_and_focus()

    def hide_to_tray(self):
        if not self._save_draft():
            return False
        self._save_preferences()
        if self.floating_panel is not None and self.floating_panel.enabled:
            self.floating_panel.conceal(self._hide_panel)
        else:
            self._hide_panel()
        return True

    def _hide_panel(self):
        self.hide()
        if hasattr(self.controller, "set_visible"):
            self.controller.set_visible(False)

    def hideEvent(self, event):
        self.timeline_hover.dismiss()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._request_latest_scroll()
        if hasattr(self.controller, "set_visible"):
            self.controller.set_visible(True)

    def closeEvent(self, event):
        if self._closing:
            event.accept()
        else:
            event.ignore()
            self.hide_to_tray()

    def request_quit(self):
        answer = QMessageBox.question(self, "退出个人时间记录", "退出后，本机尚未上传的内容会保留，等待下次打开程序继续上传。\n飞书采集和既有模型定时任务继续独立运行。\n\n现在退出？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._save_draft():
            return
        self._save_preferences()
        self._closing = True
        if self.tray:
            self.tray.hide()
        self.quit_requested.emit()
        QApplication.quit()

