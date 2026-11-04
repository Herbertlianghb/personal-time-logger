"""Reference-proportioned timeline: time geometry is independent of typography."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import math
import os

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, Signal, QSize
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QBrush
from PySide6.QtWidgets import QWidget

TAIPEI = timezone(timedelta(hours=8))
VISIBLE_CATEGORIES = ("工作", "生活", "娱乐", "学习", "休息", "运动")


def dark_theme():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            pass
    app = QGuiApplication.instance()
    return bool(app and app.styleHints().colorScheme() == Qt.ColorScheme.Dark)


def palette_tokens(dark=None):
    dark = dark_theme() if dark is None else dark
    pairs = {
        "surface": ("#ffffff", "#202329"), "canvas": ("#f6f7f8", "#282c33"),
        "ink": ("#20252b", "#eef1f4"), "muted": ("#606975", "#aeb8c5"),
        "line": ("#dfe4e9", "#414852"), "primary": ("#295b50", "#a8d6c5"),
        "primary_ink": ("#ffffff", "#173d32"), "work_fill": ("#dcece4", "#304b40"),
        "life_fill": ("#ece4d7", "#51483c"), "ai": ("#775887", "#c8abd8"),
        "ai_fill": ("#d8c8e2", "#655075"),
    }
    return {name: values[bool(dark)] for name, values in pairs.items()}


def clock(ms, include_date=False):
    if ms is None:
        return "未知"
    return datetime.fromtimestamp(ms / 1000, TAIPEI).strftime("%m-%d %H:%M" if include_date else "%H:%M")


def duration(seconds):
    if seconds is None:
        return "未知"
    seconds = max(0, float(seconds))
    if 0 < seconds < 1:
        return "不足1秒"
    if seconds < 60:
        whole = math.floor(seconds + .5)
        if whole < 60:
            return ("约" if abs(seconds - whole) > .000001 else "") + f"{whole}秒"
        return "约1分钟"
    mins = math.floor(seconds / 60 + .5)
    prefix = "约" if abs(seconds - mins * 60) > .000001 else ""
    if mins < 60:
        return prefix + f"{mins}分钟"
    hours, rest = divmod(mins, 60)
    return prefix + f"{hours}小时" + (f"{rest}分" if rest else "")


@dataclass(frozen=True)
class TimeScale:
    start_ms: int
    end_ms: int
    pixels_per_minute: float = 1.0
    top: float = 38.0

    def y(self, timestamp):
        return self.top + (timestamp - self.start_ms) / 60000 * self.pixels_per_minute

    def height(self, start, end):
        return (end - start) / 60000 * self.pixels_per_minute

    @property
    def total_height(self):
        return self.y(self.end_ms) + 24


def label_groups(anchors, minimum_distance=28):
    """Labels may cluster; the underlying event coordinates are never moved."""
    groups = []
    for anchor in sorted(anchors, key=lambda a: (a["y"], str(a.get("key", a["item"].get("id", ""))))):
        if groups and anchor["y"] - groups[-1][0]["y"] < minimum_distance:
            groups[-1].append(anchor)
        else:
            groups.append([anchor])
    return groups


def _union(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _pieces(item, field, left, right):
    result = []
    if item.get("status") in {"recalled", "cancelled", "superseded"}:
        return result
    for value in item.get(field, []):
        if isinstance(value, (list, tuple)) and len(value) == 2:
            start, end = value
        elif isinstance(value, dict):
            start, end = value.get("start_ms"), value.get("end_ms")
        else:
            continue
        if type(start) is not int or type(end) is not int:
            continue
        a, b = max(start, left), min(end, right)
        if a < b:
            result.append([a, b])
    return result


def _sleep_estimates(item, left, right):
    """Consume the server's validated display overlay; never infer from text."""
    result = []
    for estimate in item.get("estimated_intervals", []):
        if not isinstance(estimate, dict) or estimate.get("kind") != "sleep_report_window":
            continue
        start, end = estimate.get("start_ms"), estimate.get("end_ms")
        if type(start) is int and type(end) is int and start < end:
            a, b = max(left, start), min(right, end)
            if a < b:
                result.append({**estimate, "start_ms": a, "end_ms": b})
    return result


class TimelineWidget(QWidget):
    activities_clicked = Signal(list)  # compatibility; selection uses the node selector
    tag_hovered = Signal(QPoint, list, QRect)
    tag_left = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = {}
        self.colors = palette_tokens(False)
        self.scale = TimeScale(0, 86400000, 3)
        self.anchors, self.bars, self.groups, self.gaps = [], [], [], []
        self.selected_key = None
        self.tags = []
        self._hovered_tag = None
        self.setMouseTracking(True)
        self.setMinimumWidth(240)
        self.setAccessibleName("按真实时间比例排列的时间轴；通过查看节点选择详情")

    def sizeHint(self):
        return QSize(390, int(self.scale.total_height))

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        left = snapshot.get("day_start_ms", 0)
        end = snapshot.get("day_end_ms", left + 86400000)
        if type(snapshot.get("display_end_ms")) is int:
            end = max(left, min(end, snapshot["display_end_ms"]))
        # The visible range may start at the first meaningful hour; its label
        # is explicit. Nothing inside that range is compressed or omitted.
        positions = []
        for item in snapshot.get("activities", snapshot.get("items", [])):
            if item.get("status") in {"recalled", "cancelled", "superseded"}:
                continue
            for key in ("start_ms", "end_ms", "report_ms"):
                value = item.get(key)
                if type(value) is int and left <= value <= end:
                    positions.append(value)
            for field in ("recorded_intervals", "human_intervals", "ai_intervals"):
                for a, b in _pieces(item, field, left, end):
                    positions.extend([a, b])
            for estimate in _sleep_estimates(item, left, end):
                positions.extend([estimate["start_ms"], estimate["end_ms"]])
        start = max(left, min(positions) // 3600000 * 3600000) if positions else left
        self.scale = TimeScale(start, max(start, end), self.scale.pixels_per_minute)
        self._rebuild()

    def set_zoom(self, pixels_per_minute):
        value = float(pixels_per_minute)
        if not 0 < value <= 20:
            raise ValueError("invalid_time_scale")
        self.scale = TimeScale(self.scale.start_ms, self.scale.end_ms, value)
        self._rebuild()

    def events(self):
        return list(self.anchors)

    def select_event(self, key):
        self.selected_key = key if any(a["key"] == key for a in self.anchors) else None
        self.update()

    def _rebuild(self):
        self._leave_tag()
        self.anchors, self.bars, self.gaps = [], [], []
        left, right = self.scale.start_ms, self.scale.end_ms
        items = self.snapshot.get("activities", self.snapshot.get("items", []))
        occupied = []
        lane_ends, item_lanes = [], {}
        for index, item in enumerate(items):
            identity = str(item.get("id", item.get("activity_id", index)))
            if item.get("status") in {"recalled", "cancelled", "superseded"}:
                continue
            fields = [("recorded", "recorded_intervals"), ("human", "human_intervals"), ("ai", "ai_intervals")]
            for kind, field in fields:
                for a, b in _pieces(item, field, left, right):
                    if kind != "ai":
                        occupied.append([a, b])
                    self.bars.append({"kind": kind, "item": item, "start_ms": a, "end_ms": b,
                                      "y": self.scale.y(a), "height": self.scale.height(a, b), "lane": 0})
            # Known source boundaries become individual selector events. A
            # report is positioned only if both actual boundaries are unknown.
            start, end = item.get("start_ms"), item.get("end_ms")
            is_ai = item.get("actor") == "ai" or item.get("source") == "backend_accepted_ai"
            # Explicit outer bounds describe a window, not confirmed human
            # effort. Do not bridge interrupted segments when any exist.
            if (not is_ai and item.get("role", "primary") == "primary"
                    and not any(item.get(field) for _, field in fields)
                    and type(start) is int and type(end) is int):
                a, b = max(left, start), min(right, end)
                if a < b:
                    self.bars.append({"kind": "window", "item": item, "start_ms": a, "end_ms": b,
                                      "y": self.scale.y(a), "height": self.scale.height(a, b), "lane": 0})
            candidates = []
            if type(start) is int:
                if start < left and type(end) is int and end > left:
                    candidates.append((left, "continuation", "跨日延续"))
                elif left <= start < right:
                    candidates.append((start, "activity", "开始"))
            if type(end) is int and left <= end <= right:
                candidates.append((end, "end", "结束"))
            if start is None and end is None and type(item.get("report_ms")) is int:
                value = item["report_ms"]
                if left <= value <= right:
                    candidates.append((value, "report", "报告"))
            for timestamp, kind, verb in candidates:
                boundary_kind = item.get("start_kind" if kind in {"activity", "continuation"} else "end_kind")
                if boundary_kind in {"estimate", "report_anchor"}:
                    verb += "（约）" if boundary_kind == "estimate" else "（报告）"
                self.anchors.append({"key": f"{identity}:{kind}:{timestamp}", "timestamp": timestamp,
                    "y": self.scale.y(timestamp), "kind": kind, "item": item, "track": "ai" if is_ai else "human",
                    "title": f"{clock(timestamp)} · {'AI ' if is_ai else ''}{verb} · {item.get('name', '活动')}"})
            for estimate in _sleep_estimates(item, left, right):
                a, b = estimate["start_ms"], estimate["end_ms"]
                # Coverage is visual only. The source facts and the backend's
                # confirmed-human summary remain untouched.
                occupied.append([a, b])
                self.bars.append({"kind": "sleep_estimate", "item": item, "estimate": estimate,
                                  "start_ms": a, "end_ms": b, "y": self.scale.y(a),
                                  "height": self.scale.height(a, b), "lane": 0})
                display_item = {**item, "display_estimate": estimate}
                for timestamp, kind, verb in ((a, "estimated_start", "估计窗口起点"),
                                               (b, "estimated_end", "估计窗口终点")):
                    self.anchors.append({"key": f"{identity}:{kind}:{timestamp}", "timestamp": timestamp,
                        "y": self.scale.y(timestamp), "kind": kind, "item": display_item, "track": "human",
                        "title": f"{clock(timestamp)} · {verb} · 睡眠（约）"})
        # Partition only overlapping activities horizontally. The vertical
        # positions/heights retain the same clock scale for every lane.
        for track in ("human", "ai"):
            representatives = {}
            for bar in self.bars:
                if (bar["kind"] == "ai") != (track == "ai"):
                    continue
                key = str(bar["item"].get("id", id(bar["item"])))
                if key in representatives:
                    representatives[key][0] = min(representatives[key][0], bar["start_ms"])
                    representatives[key][1] = max(representatives[key][1], bar["end_ms"])
                else:
                    representatives[key] = [bar["start_ms"], bar["end_ms"]]
            lane_ends, mapping = [], {}
            for key, (a, b) in sorted(representatives.items(), key=lambda pair: pair[1]):
                lane = next((i for i, until in enumerate(lane_ends) if until <= a), len(lane_ends))
                if lane == len(lane_ends):
                    lane_ends.append(b)
                else:
                    lane_ends[lane] = b
                mapping[key] = lane
            for bar in self.bars:
                if (bar["kind"] == "ai") == (track == "ai"):
                    bar["lane"] = mapping[str(bar["item"].get("id", id(bar["item"]))) ]
                    bar["lane_count"] = max(1, len(lane_ends))
        self.anchors.sort(key=lambda a: (a["timestamp"], a["key"]))
        self.groups = label_groups(self.anchors)
        if self.anchors or self.bars:
            cursor = left
            for a, b in _union(occupied):
                if cursor < a:
                    self.gaps.append({"start_ms": cursor, "end_ms": a, "y": self.scale.y(cursor), "height": self.scale.height(cursor, a)})
                cursor = max(cursor, b)
            if cursor < right:
                self.gaps.append({"start_ms": cursor, "end_ms": right, "y": self.scale.y(cursor), "height": self.scale.height(cursor, right)})
        if self.selected_key and not any(a["key"] == self.selected_key for a in self.anchors):
            self.selected_key = None
        self.setFixedHeight(math.ceil(self.scale.total_height) if self.anchors or self.bars else 130)
        self.updateGeometry()
        self.update()

    def first_y(self):
        return min((a["y"] for a in self.anchors), default=38)

    def _font(self, size=12):
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(size)
        return font

    def _clock_labels(self):
        """Thin text only; every event retains its exact timestamp and y."""
        labels = []
        ordered = sorted(self.anchors, key=lambda a: (a["key"] != self.selected_key,
                         a["timestamp"], a["track"] == "ai", a["key"]))
        for anchor in ordered:
            if all(abs(anchor["y"] - other["y"]) >= 18 for other in labels):
                labels.append(anchor)
        return labels

    def _category_tags(self):
        """Declutter labels horizontally; never move the underlying events."""
        tags = []
        lane_left, lane_right = 84.0, self.width() - 76.0
        for group in label_groups(self.anchors, 22):
            categories = {}
            for anchor in group:
                category = anchor["item"].get("category")
                if category not in VISIBLE_CATEGORIES:
                    continue
                category_group = categories.setdefault(category, [])
                identity = anchor["item"].get("id", anchor["item"].get("activity_id", anchor["key"]))
                if not any(entry["identity"] == identity for entry in category_group):
                    category_group.append({"identity": identity, "anchor": anchor})
            x = lane_right
            for category, entries in categories.items():
                text = category + (f" ·{len(entries)}" if len(entries) > 1 else "")
                width = max(43.0, self.fontMetrics().horizontalAdvance(text) + 14.0)
                x -= width
                if x < lane_left:
                    # All source nodes remain in the selector, including tags
                    # that do not fit this dense group at the current zoom.
                    break
                anchor = entries[0]["anchor"]
                tags.append({"category": category, "text": text, "y": anchor["y"],
                             "rect": QRectF(x, anchor["y"] - 10, width, 20),
                             "items": [entry["anchor"]["item"] for entry in entries]})
                x -= 5
        return tags

    def _leave_tag(self):
        if self._hovered_tag is not None:
            self._hovered_tag = None
            self.tag_left.emit()

    def mouseMoveEvent(self, event):
        tag = next((tag for tag in self.tags if tag["rect"].contains(event.position())), None)
        if tag is None:
            self._leave_tag()
        else:
            rect = tag["rect"].toAlignedRect()
            global_rect = QRect(self.mapToGlobal(rect.topLeft()), rect.size())
            identity = (tag["category"], global_rect.x(), global_rect.y(),
                        tuple(str(item.get("id", item.get("activity_id", ""))) for item in tag["items"]))
            if self._hovered_tag != identity:
                self._hovered_tag = identity
                self.tag_hovered.emit(event.globalPosition().toPoint(), tag["items"], global_rect)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._leave_tag()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        self.tags = self._category_tags()
        self._leave_tag()
        super().resizeEvent(event)

    def _caption_width(self, x, center, width):
        for tag in self.tags:
            if abs(tag["y"] - center) < 28 and tag["rect"].right() > x:
                width = min(width, tag["rect"].left() - x - 5)
        return max(0, width)

    def _estimate_caption_center(self, bar):
        visible = self.visibleRegion().boundingRect()
        if visible.isEmpty():
            visible = self.rect()
        top = max(bar["y"], visible.top())
        bottom = min(bar["y"] + bar["height"], visible.bottom())
        return (top + bottom) / 2 if bottom - top >= 34 else None

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.colors
        p.fillRect(self.rect(), QColor(c["surface"]))
        p.setFont(self._font())
        p.setPen(QColor(c["muted"]))
        width = self.width()
        self.tags = self._category_tags()
        human_x, bar_x, ai_x = 75.0, 84.0, width - 24.0
        bar_width = max(60.0, width - 160.0)
        p.drawText(QPointF(0, 15), "记录时刻")
        p.drawText(QPointF(bar_x, 15), "活动 / 生活")
        p.drawText(QRectF(ai_x - 20, 0, 40, 22), Qt.AlignmentFlag.AlignHCenter, "AI")
        if not self.anchors and not self.bars:
            p.drawText(QRectF(0, 40, width, 55), Qt.AlignmentFlag.AlignCenter,
                       "没有可定位的实际时间\n未知与估算记录保留在下方，不补造区间")
            p.end()
            return
        p.setPen(QPen(QColor(c["line"]), 1))
        p.drawLine(QPointF(human_x, self.scale.top), QPointF(human_x, self.scale.y(self.scale.end_ms)))
        actual_labels = self._clock_labels()
        step = (15 if self.scale.pixels_per_minute >= 6 else
                60 if self.scale.pixels_per_minute * 60 >= 30 else
                120 if self.scale.pixels_per_minute * 120 >= 30 else 240)
        interval = step * 60000
        minute = math.ceil(self.scale.start_ms / interval) * interval
        while minute <= self.scale.end_ms:
            y = self.scale.y(minute)
            p.setPen(QPen(QColor(c["line"]), 1))
            p.drawLine(QPointF(54, y), QPointF(width - 8, y))
            if all(abs(y - anchor["y"]) >= 18 for anchor in actual_labels):
                p.setPen(QColor(c["muted"]))
                p.drawText(QPointF(0, y + 4), clock(minute))
            minute += step * 60000
        for anchor in actual_labels:
            p.setPen(QColor(c["ai"] if anchor["track"] == "ai" else c["primary"]))
            p.drawText(QPointF(0, anchor["y"] + 4), clock(anchor["timestamp"]))
        # Window bounds retain the unknown hatch beneath them. Reserve their
        # caption area so a gap caption cannot print through the same text.
        window_captions = []
        for bar in self.bars:
            if bar["kind"] == "window" and bar["height"] >= 34:
                w = bar_width / bar.get("lane_count", 1)
                x = bar_x + w * bar["lane"]
                center = bar["y"] + bar["height"] / 2
                window_captions.append(QRectF(x + 7, center - 16, max(0, w - 14), 34))
        for gap in self.gaps:
            rect = QRectF(bar_x, gap["y"], bar_width, gap["height"])
            p.fillRect(rect, QBrush(QColor(c["line"]), Qt.BrushStyle.BDiagPattern))
            if gap["height"] >= 46:
                center = gap["y"] + gap["height"] / 2
                caption_rect = QRectF(bar_x + 7, center - 16, max(0, bar_width - 14), 34)
                if not any(caption_rect.intersects(other) for other in window_captions):
                    p.setPen(QColor(c["muted"]))
                    caption_width = int(self._caption_width(bar_x + 7, center, bar_width - 14))
                    for offset, text in ((-2, "时间信息待补充"), (14, clock(gap["start_ms"]) + "—" + clock(gap["end_ms"]))):
                        p.drawText(QPointF(bar_x + 7, center + offset), p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, caption_width))
        ai_captions = []
        for bar in sorted(self.bars, key=lambda b: b["kind"] == "human"):
            # A recorded interval already carries its display shape. The
            # contained effort evidence affects summaries, not a second bar.
            if bar["kind"] == "human" and bar["item"].get("recorded_intervals"):
                continue
            ai = bar["kind"] == "ai"
            lanes = bar.get("lane_count", 1)
            if ai:
                x, w = ai_x - 5 + bar["lane"] * (12 / lanes), 10 / lanes
                fill = c["ai_fill"]
            else:
                w = bar_width / lanes
                x = bar_x + w * bar["lane"]
                fill = c["life_fill"] if bar["item"].get("category") in {"生活", "娱乐", "休息", "运动"} else c["work_fill"]
            rect = QRectF(x, bar["y"], w, bar["height"])
            if bar["kind"] in {"window", "sleep_estimate"}:
                if bar["kind"] == "sleep_estimate":
                    p.fillRect(rect, QColor(c["life_fill"]))
                p.setPen(QPen(QColor(c["primary"]), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect)
            else:
                p.fillRect(rect, QColor(fill))  # deliberately no minimum bar height
            if bar["height"] >= 34:
                p.setPen(QColor(c["ink"]))
                if ai:
                    label = duration((bar["end_ms"] - bar["start_ms"]) / 1000)
                    caption_rect = QRectF(ai_x - 72, bar["y"] + bar["height"] / 2 - 10, 61, 20)
                    if not any(caption_rect.intersects(other) for other in ai_captions):
                        p.drawText(caption_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
                        ai_captions.append(caption_rect)
                elif bar["kind"] != "human" or not bar["item"].get("recorded_intervals"):
                    estimated = bar["kind"] == "sleep_estimate"
                    center = self._estimate_caption_center(bar) if estimated else bar["y"] + bar["height"] / 2
                    if center is None:
                        continue
                    caption_width = int(self._caption_width(x + 7, center, w - 14))
                    title = p.fontMetrics().elidedText("睡眠（约）" if estimated else bar["item"].get("name", "活动"), Qt.TextElideMode.ElideRight, caption_width)
                    p.drawText(QPointF(x + 7, center - 2), title)
                    suffix = duration((bar["end_ms"] - bar["start_ms"]) / 1000)
                    if bar["kind"] == "recorded":
                        suffix += "记录区间"
                    elif bar["kind"] == "window":
                        suffix = "活动窗口 · 人工未知"
                    elif estimated and not suffix.startswith("约"):
                        suffix = "约" + suffix
                    p.setPen(QColor(c["muted"]))
                    suffix = p.fontMetrics().elidedText(suffix, Qt.TextElideMode.ElideRight, caption_width)
                    p.drawText(QPointF(x + 7, center + 14), suffix)
        for anchor in self.anchors:
            is_ai = anchor["track"] == "ai"
            x, color = (ai_x, c["ai"]) if is_ai else (human_x, c["primary"])
            selected = anchor["key"] == self.selected_key
            p.setPen(QPen(QColor(color), 2 if selected else 1))
            p.setBrush(QColor(c["surface"]) if selected or anchor["kind"] == "report" else QColor(color))
            p.drawEllipse(QPointF(x, anchor["y"]), 4.5 if selected else 3.5, 4.5 if selected else 3.5)
            if selected:
                p.drawLine(QPointF(x - 9, anchor["y"]), QPointF(x - 4, anchor["y"]))
        for tag in self.tags:
            p.setPen(QPen(QColor(c["primary"]), .8))
            p.setBrush(QColor(c["surface"]))
            p.drawRoundedRect(tag["rect"], 6, 6)
            p.drawText(tag["rect"], Qt.AlignmentFlag.AlignCenter, tag["text"])
        p.end()
