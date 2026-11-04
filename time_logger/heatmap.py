"""Read-only recorded windows by month or rolling year, separate from effort.

The complete bounded Base read is cached in the existing evidence Store.  The
response contains daily aggregates only; activity details use the day history
endpoint so a busy month does not exceed the desktop transport response bound.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, time, timedelta

from .base_adapter import ALLOWLIST, HttpBaseTransport, _schema_fields
from .calculator import LOCAL_TZ, seconds
from .store import digest, now_ms
from .day_window import DAY_START_HOUR, day_bounds, recording_date
from .timeline import (_ai_items, _claims, _clip, _date_anchor, _evidence,
                       _item, _local_state, _merge_items, _query, _relevant,
                       _timestamp)


CATEGORIES = ("工作", "生活", "娱乐", "学习", "休息", "运动")
CACHE_TTL_MS = 60000
DISPLAY_VERSION = 5
_MONTH = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_EXPLICIT_AI_WINDOW = re.compile(
    r"^(?:(?:AI|模型|机器)\s*(?:后台)?执行窗口|(?:纯|仅)(?:AI|模型|机器)\s*(?:后台)?执行(?:窗口)?)(?:$|[\s:：·—（(])",
    re.IGNORECASE)
_ESTIMATED_WINDOW = re.compile(r"估算|近似|非精确|大约|约\s*\d+\s*(?:分|小时|时|点)")
_COMPLETION_MARKER = "[time-log-completion:2026-10-28]"


def month_bounds(month):
    if not isinstance(month, str) or not _MONTH.fullmatch(month):
        raise ValueError("invalid_heatmap_month")
    try:
        first = date.fromisoformat(month + "-01")
        following = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    except (ValueError, OverflowError):
        raise ValueError("invalid_heatmap_month") from None
    if first.year < 2000 or following.year > 2100:
        raise ValueError("invalid_heatmap_month")
    return first, following


def period_bounds(month, scope="month"):
    if scope not in ("month", "year"):
        raise ValueError("invalid_heatmap_scope")
    first, following = month_bounds(month)
    if scope == "year":
        index = first.year * 12 + first.month - 1 - 11
        first = max(date(2000, 1, 1), date(index // 12, index % 12 + 1, 1))
    return first, following


def _stamp(day):
    return day_bounds(day.isoformat(), DAY_START_HOUR)[0]


def _ai_window(item, blocks):
    # The presence of the word AI alone does not identify an execution window:
    # a person may be writing prompts, reviewing results or using an AI tool.
    block = blocks.get(item.get("activity_id"), {})
    _, claims = _claims({"备注": item.get("notes", "")})
    return (item.get("actor") == "ai" or block.get("actor") == "ai"
            or item.get("source") == "backend_accepted_ai"
            or (claims or {}).get("actor") == "ai"
            or bool(_EXPLICIT_AI_WINDOW.search(item.get("name", "").strip())))


def _heatmap_item(item, fields, left, right):
    # The day timeline deliberately hides approximate legacy boundaries. Heat
    # uses the saved Base window, but keeps its uncertainty separate from both
    # definite windows and evidence of active human effort. Never turn a scalar
    # duration or a report-only sleep estimate into a new interval here.
    start, end = _timestamp(fields.get("开始时间")), _timestamp(fields.get("结束时间"))
    window = _clip([[start, end]], left, right) if start is not None and end is not None else []
    notes = str(fields.get("备注") or "")
    estimated = bool(item.get("estimated") or _COMPLETION_MARKER in notes or _ESTIMATED_WINDOW.search(notes))
    _, claims = _claims(fields)
    estimated = estimated or any((claims or {}).get(key, {}).get("kind") in {"estimate", "report_anchor"}
                                 for key in ("start", "end"))
    item["heatmap_estimated_intervals"] = window if estimated else []
    item["recorded_intervals"] = [] if estimated else window
    return item


def _merge_heatmap_items(items):
    # Preserve estimate windows across Base segments of one activity without
    # changing the shared timeline representation or its other consumers.
    estimates = {}
    for item in items:
        estimates.setdefault(item["id"], []).extend(item.get("heatmap_estimated_intervals", []))
    merged = _merge_items(items)
    for item in merged:
        item["heatmap_estimated_intervals"] = estimates[item["id"]]
    return merged


def _day_items(items, left, right):
    selected = []
    for item in items:
        intervals = (item.get("recorded_intervals", []) + item.get("heatmap_estimated_intervals", [])
                     + item.get("human_intervals", []) + item.get("ai_intervals", []))
        overlaps = any(max(a, left) < min(b, right) for a, b in intervals)
        anchor = item.get("report_ms") if item.get("report_ms") is not None else item.get("received_ms")
        start = item.get("start_ms") if item.get("start_ms") is not None else item.get("original_start_ms")
        end = item.get("end_ms") if item.get("end_ms") is not None else item.get("original_end_ms")
        if overlaps or _relevant(start, end, anchor, left, right):
            selected.append(item)
    return selected


def _totals(items, left, right, blocks):
    active = [item for item in items if item.get("role") != "detail"
              and item.get("status") not in {"recalled", "cancelled", "superseded"}]
    ai = [item for item in active if _ai_window(item, blocks)]
    included = [item for item in active if not _ai_window(item, blocks)]
    confirmed = _clip([piece for item in included for piece in item.get("recorded_intervals", [])], left, right)
    estimated = _clip([piece for item in included for piece in item.get("heatmap_estimated_intervals", [])], left, right)
    recorded = _clip(confirmed + estimated, left, right)
    human = _clip([piece for item in included for piece in item.get("human_intervals", [])], left, right)
    confirmed_seconds = seconds(confirmed) if confirmed else 0
    recorded_seconds = seconds(recorded) if recorded else 0
    # Confirmed windows take precedence at overlaps; the estimate component is
    # the remaining part of the union, never an additive sum of record spans.
    estimated_seconds = recorded_seconds - confirmed_seconds
    return {"recorded_seconds": seconds(recorded) if recorded else None,
            "confirmed_seconds": confirmed_seconds if confirmed else None,
            "estimated_seconds": estimated_seconds,
            "estimated_count": sum(bool(_clip(item.get("heatmap_estimated_intervals", []), left, right)) for item in included),
            "human_seconds": seconds(human) if human else None,
            "record_count": len(included),
            "unknown_count": sum(not _clip(item.get("recorded_intervals", []) + item.get("heatmap_estimated_intervals", []), left, right)
                                 for item in included),
            "excluded_ai_count": len(ai)}


class HeatmapReader:
    def __init__(self, store, transport, *, cache_ttl_ms=CACHE_TTL_MS, clock=now_ms):
        self.store, self.transport, self.cache_ttl_ms, self.clock = store, transport, cache_ttl_ms, clock

    def _fetch(self, month, timestamp, local_state, scope="month"):
        first, following = period_bounds(month, scope)
        left, month_right = _stamp(first), _stamp(following)
        right = min(month_right, timestamp)
        schema = _schema_fields(self.transport.field_schema())
        if any(schema.get(name, {}).get("type") != kind for name, kind in
               (("时间花销", "text"), ("开始时间", "datetime"), ("结束时间", "datetime"), ("备注", "text"))):
            raise ValueError("heatmap_schema_mismatch")
        projected = [name for name in ALLOWLIST if name in schema]
        blocks, mappings = local_state
        mapping_by_id = {mapping["remote_id"]: mapping for mapping in mappings}
        found = {}
        if right > left:
            previous = (first - timedelta(days=1)).isoformat()
            after = (following + timedelta(days=1)).isoformat()
            # Date predicates are deliberately wider than the month boundary.
            # Exact milliseconds are compared locally, as in the day timeline.
            predicates = [
                {"logic": "and", "conditions": [[field, ">", f"ExactDate({previous})"],
                                                    [field, "<", f"ExactDate({after})"]]}
                for field in ("开始时间", "结束时间")]
            predicates.append({"logic": "and", "conditions": [
                ["开始时间", "<", f"ExactDate({after})"],
                ["结束时间", ">", f"ExactDate({previous})"]]})
            for predicate in predicates:
                for row in _query(self.transport, predicate, projected):
                    if row["record_id"] in found and found[row["record_id"]]["fields"] != row["fields"]:
                        raise ValueError("heatmap_remote_changed_during_read")
                    found[row["record_id"]] = row
            untimed_ids = []
            for mapping in mappings:
                baseline = json.loads(mapping["baseline"]) if mapping.get("baseline") else {}
                if _timestamp(baseline.get("开始时间")) is not None or _timestamp(baseline.get("结束时间")) is not None:
                    continue
                _, claims = _claims(baseline)
                evidence = _evidence(self.store, (claims or {}).get("evidence_refs", []))
                if _relevant(None, None, _date_anchor(evidence), left, right) and mapping["remote_id"] not in found:
                    untimed_ids.append(mapping["remote_id"])
            if untimed_ids:
                if hasattr(self.transport, "get_many"):
                    untimed = self.transport.get_many(untimed_ids)
                else:
                    untimed = [self.transport.get(rid) for rid in dict.fromkeys(untimed_ids)]
                for row in untimed:
                    if row:
                        found[row["record_id"]] = row
        items = []
        for row in found.values():
            item = _item(row["fields"], row["record_id"], mapping_by_id.get(row["record_id"]),
                         blocks, schema, self.store, left, right, timestamp)
            if item:
                items.append(_heatmap_item(item, row["fields"], left, right))
        items = _merge_heatmap_items(items) + _ai_items(self.store, blocks, left, right)
        days, current = [], first
        while current < following:
            day_left, day_right = _stamp(current), _stamp(current + timedelta(days=1))
            selected = _day_items(items, day_left, min(day_right, timestamp)) if day_left < timestamp else []
            total = _totals(selected, day_left, min(day_right, timestamp), blocks)
            by_category = {}
            for category in (*CATEGORIES, "unknown"):
                filtered = [item for item in selected if
                            (item.get("category") == category if category != "unknown" else item.get("category") not in CATEGORIES)]
                by_category[category] = _totals(filtered, day_left, min(day_right, timestamp), blocks)
            days.append({"date": current.isoformat(), "future": day_left >= timestamp,
                         **total, "categories": by_category})
            current += timedelta(days=1)
        raw_hash = digest(sorted((rid, row["fields"]) for rid, row in found.items()))
        summary = _totals(items, left, right, blocks)
        summary["categories"] = {
            category: _totals([item for item in items if
                              (item.get("category") == category if category != "unknown" else item.get("category") not in CATEGORIES)],
                             left, right, blocks)
            for category in (*CATEGORIES, "unknown")}
        return {"month": month, "scope": scope, "day_start_hour": DAY_START_HOUR,
                "range_start_ms": left, "range_end_ms": month_right,
                "range_start": first.isoformat(), "range_end": following.isoformat(),
                "timezone": "Asia/Taipei", "as_of_ms": timestamp,
                "server_now_ms": timestamp, "days": days, "summary": summary,
                "data_revision": digest({"source_hash": raw_hash, "days": days}),
                "completeness": "partial", "source_snapshot_atomic": False,
                "limitations": ["色深表示确定与估算的已记录区间并集，不等于净人工投入。", "明确AI执行窗口排除；使用AI工具本身不代表后台执行。",
                                "估算仅采用正式表已有双边时间，单独标记；重叠部分优先计入确定区间。",
                                "起止不全和标量时长不生成热度；无记录不代表实际为零。",
                                "无日期且没有已保存报告来源的旧记录无法归日。",
                                "分类各自去重；跨分类可能重叠，分类时长不可相加。",
                                "Base分页不提供原子快照；缓存保存完整有界读取结果。"]}

    def month(self, month, *, refresh=False, scope="month"):
        first, following = period_bounds(month, scope)
        if type(refresh) is not bool:
            raise ValueError("invalid_heatmap_refresh")
        timestamp = self.clock()
        local_state = _local_state(self.store)
        source = getattr(self.transport, "table_key", None)
        source_hash = digest(source)[:24] if source is not None else "test"
        key = "desktop_heatmap_cache:" + source_hash + ":start:7:" + month
        if scope != "month":
            key += ":" + scope
        state_hash = digest({"display_version": DISPLAY_VERSION, "scope": scope, "mappings": local_state[1],
                             "activities": [(key, value["revision"]) for key, value in sorted(local_state[0].items())]})
        cached = self.store.get_setting(key)
        if cached and (cached.get("month") != month or cached.get("scope", "month") != scope
                       or cached.get("day_start_hour") != DAY_START_HOUR):
            cached = None
        cache_state, error = "fresh", None
        # Refresh requests still honor the bounded minimum interval, so repeated
        # button clicks cannot repeatedly scan Base within one minute.
        if (cached and cached.get("local_state_hash") == state_hash
                and 0 <= timestamp - cached["as_of_ms"] < self.cache_ttl_ms):
            snapshot, cache_state = cached, "cached"
        else:
            try:
                snapshot = self._fetch(month, timestamp, local_state, scope)
                snapshot["local_state_hash"] = state_hash
                self.store.set_setting(key, snapshot)
            except Exception as exc:
                if not cached:
                    raise
                snapshot, cache_state, error = cached, "stale", type(exc).__name__
        result = copy.deepcopy(snapshot)
        result.pop("local_state_hash", None)
        # A previous month-only cache is a valid outage fallback for that same
        # month, but can never be returned as a rolling-year response.
        result.update(scope=scope, range_start=first.isoformat(), range_end=following.isoformat())
        result["server_now_ms"] = timestamp
        result["cache"] = {"state": cache_state, "as_of_ms": snapshot["as_of_ms"], "refresh_error": error,
                           "min_refresh_interval_ms": self.cache_ttl_ms}
        return result


def monthly_heatmap(store, config, month, *, refresh=False, scope="month"):
    from .feishu import Feishu
    from .daily_goals import DailyGoalReader
    base = config.get("base_token")
    table = config.get("time_table_id") or config.get("table_id")
    if not isinstance(base, str) or not base or not isinstance(table, str) or not table:
        raise ValueError("desktop_formal_source_not_configured")
    platform = Feishu(config["app_id"], config["app_secret"])
    transport = HttpBaseTransport(platform, base, table)
    result = HeatmapReader(store, transport).month(month, refresh=refresh, scope=scope)
    today = recording_date(datetime.fromtimestamp(result["server_now_ms"] / 1000, LOCAL_TZ)).isoformat()
    standards = config.get("time_standards_table_id")
    if standards:
        goals_transport = HttpBaseTransport(platform, base, standards)
        result["daily_goals"] = DailyGoalReader(store, goals_transport,
            settings_url=f"https://my.feishu.cn/base/{base}?table={standards}").read(today)
    else:
        result["daily_goals"] = {"date": today, "source": "feishu", "work_hours": None,
            "study_hours": None, "cache": {"state": "unavailable", "refresh_error": "not_configured"}}
    return result
