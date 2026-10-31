"""Read-only projections of immutable desktop submissions, without inference."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, time, timedelta
import json

from .calculator import LOCAL_TZ
from .desktop_bridge import BridgeError, _date_range
from .heatmap import CATEGORIES, period_bounds
from .store import digest, now_ms, packed


def _organized_item(store, block, left, right):
    from .timeline import _clip, _relevant, _report_ms, _received_ms
    from .calculator import seconds
    evidence = store.evidence(block.get("evidence_refs", []))
    if block.get("_acceptance_test") or any(e.get("is_acceptance_test") for e in evidence):
        return None
    start, end = block.get("start", {}).get("value_ms"), block.get("end", {}).get("value_ms")
    # Keep an offline device's reported date visible, while never using an
    # unverified report timestamp as an activity boundary or human interval.
    reported = _report_ms(evidence)
    unverified = reported is None
    if reported is None:
        reported = max((e["created_at_ms"] for e in evidence if type(e.get("created_at_ms")) is int), default=None)
    if not _relevant(start, end, reported, left, right):
        return None
    excluded = block.get("status") in {"recalled", "cancelled", "superseded"}
    window = _clip([[start, end]], left, right) if type(start) is int and type(end) is int and not excluded else []
    ai = block.get("actor") == "ai"
    estimated = any(block.get(key, {}).get("kind") == "estimate" for key in ("start", "end"))
    human = _clip([[piece["start_ms"], piece["end_ms"]] for piece in block.get("human_intervals", [])], left, right)
    human = human if block.get("actor") == "human" and block.get("role") == "primary" and not excluded else []
    return {"id": "activity:" + block["activity_id"] + ":" + str(block["revision"]),
            "activity_id": block["activity_id"], "revision": block["revision"], "name": block["name"],
            "record_ids": [], "record_id": None, "editable": False,
            "start_ms": None if block.get("start", {}).get("kind") == "estimate" else start,
            "end_ms": None if block.get("end", {}).get("kind") == "estimate" else end,
            "original_start_ms": start, "original_end_ms": end,
            "report_ms": reported, "received_ms": _received_ms(evidence),
            "recorded_intervals": [] if ai or estimated else window,
            "heatmap_estimated_intervals": window if estimated and not ai else [],
            "estimated_intervals": [],
            "human_intervals": human, "ai_intervals": window if ai else [],
            "duration_seconds": seconds(window) if window and not estimated else None,
            "duration_kind": "estimated_window" if estimated else "recorded_window" if window else "unknown",
            "reported_duration_seconds": block.get("reported_duration_seconds"), "estimated": estimated,
            "role": block.get("role"), "actor": block.get("actor"), "status": block.get("status"),
            "category": block.get("category"), "project": block.get("project"),
            "source": "Codex 按需整理", "formal_state": "manual_organized",
            "source_refs": [{"type": "evidence", "id": e["id"], "report_ms": e.get("created_at_ms"),
                             "received_ms": e.get("observed_at_ms")} for e in evidence],
            "result": block.get("result", ""), "subtasks": block.get("subtasks", []), "notes": "",
            "uncertainty": block.get("uncertainty", "") + ("；设备报告时刻未经服务端时钟核实。" if unverified else ""),
            "parent_activity_id": block.get("parent_activity_id")}


def _item(row):
    return {"id": "raw:" + row["observation_id"], "name": row["raw_text"],
            "observation_id": row["observation_id"], "record_ids": [], "record_id": None,
            "activity_id": None, "revision": None, "editable": False,
            "start_ms": None, "end_ms": None, "report_ms": row["reported_at_ms"],
            "received_ms": row["server_saved_at_ms"], "original_start_ms": None, "original_end_ms": None,
            "recorded_intervals": [], "human_intervals": [], "ai_intervals": [], "estimated_intervals": [],
            "duration_seconds": None, "reported_duration_seconds": None, "duration_kind": "unknown",
            "estimated": False, "role": "primary", "actor": "unknown", "status": "unknown",
            "category": None, "project": None, "source": "desktop_raw", "formal_state": "not_applicable",
            "source_refs": [{"type": "evidence", "id": row["observation_id"],
                             "report_ms": row["reported_at_ms"], "received_ms": row["server_saved_at_ms"]}],
            "result": "", "subtasks": [], "notes": "", "uncertainty": "已保存原文；报告时刻不代表活动起止或时长。"}


def history(store, day, cursor=None, limit=100):
    left, right = _date_range(day)
    timestamp = now_ms()
    offset, watermark, as_of = 0, None, timestamp
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            offset, watermark, as_of = token["offset"], token["watermark"], token["as_of_ms"]
            if (token["date"] != day or token["scope"] != store.scope
                    or type(offset) is not int or not 0 <= offset <= 20000
                    or type(watermark) is not int or not 0 <= watermark < 2 ** 63
                    or type(as_of) is not int or not 0 < as_of <= timestamp + 1000):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
            raise BridgeError("timeline_cursor_expired") from None
    # Append-only rowid watermark keeps page contents stable if another
    # device uploads an older offline record between requests.
    with store.transaction() as db:
        blocks = store.current_activities()
        activity_hash = digest(blocks)
        if cursor and token.get("activity_hash", activity_hash) != activity_hash:
            raise BridgeError("timeline_cursor_expired")
        if watermark is None:
            watermark = db.execute("SELECT coalesce(max(rowid),0) FROM desktop_submissions").fetchone()[0]
        where = "rowid<=? AND reported_at_ms>=? AND reported_at_ms<?"
        params = (watermark, left, right)
        raw_total = db.execute("SELECT count(*) FROM desktop_submissions WHERE " + where, params).fetchone()[0]
        if raw_total > 20000:
            raise BridgeError("history_day_limit")
        rows = db.execute("SELECT * FROM desktop_submissions WHERE " + where
                          + " ORDER BY reported_at_ms,rowid", params).fetchall()
        organized = [item for block in blocks if (item := _organized_item(store, block, left, right)) is not None]
        covered = {ref for block in blocks for ref in block.get("evidence_refs", [])}
        raw = [_item(row) for row in rows if row["observation_id"] not in covered]
        all_items = sorted(organized + raw, key=lambda item: (item.get("start_ms") or item.get("report_ms") or left, item["id"]))
        total = len(all_items)
        if offset > total:
            raise BridgeError("timeline_cursor_expired")
        items = all_items[offset:offset + limit]
    summary = {"human_seconds": None, "work_study_seconds": None, "other_seconds": None,
               "recorded_seconds": None, "work_study_recorded_seconds": None, "ai_seconds": None,
               "scalar_seconds": None, "estimated_seconds": None, "scalar_is_additive": False,
               "unknown_count": total, "formal_record_count": 0, "raw_record_count": total,
               "completeness": "raw_only", "scope": "仅原文和报告时刻；未推算起止、时长、分类或人工投入。"}
    if organized:
        from .timeline import _summary
        from .calculator import seconds
        active = [item for item in organized if item.get("role") != "detail" and item.get("status") not in {"recalled", "cancelled", "superseded"}]
        definite = [part for item in active for part in item["recorded_intervals"]]
        approximate = [part for item in active for part in item["heatmap_estimated_intervals"]]
        summary = {**_summary(all_items), "raw_record_count": len(raw), "organized_count": len(organized),
                   "estimated_window_seconds": seconds(definite + approximate) - seconds(definite),
                   "scope": "Codex 按需整理；起止与分类有原文来源，未知保留；记录区间不等于净人工投入。"}
    next_cursor = None
    if offset + len(items) < total:
        next_cursor = base64.urlsafe_b64encode(packed({"date": day, "scope": store.scope,
            "watermark": watermark, "activity_hash": activity_hash, "as_of_ms": as_of, "offset": offset + len(items)}).encode("utf-8")).decode("ascii")
    revision = digest({"scope": store.scope, "date": day, "watermark": watermark, "activities": activity_hash, "total": total})
    return {"mode": "desktop_organized" if organized else "desktop_only", "date": day, "timezone": "Asia/Taipei", "day_start_ms": left,
            "day_end_ms": right, "display_end_ms": max(left, min(right, as_of)), "as_of_ms": as_of,
            "server_now_ms": timestamp, "data_revision": revision, "source_hash": revision,
            "items": items, "summary": summary, "total_items": total, "next_cursor": next_cursor,
            "completeness": "partial" if organized else "raw_only", "source_snapshot_atomic": True,
            "cache": {"state": "fresh", "as_of_ms": as_of},
            "limitations": ["报告时刻按设备提交时间展示，不代表活动起止。", "仅在本人要求时由 Codex 整理；未接飞书，未知起止不补造。"]}


def _counts(count):
    return {"recorded_seconds": None, "confirmed_seconds": None, "estimated_seconds": 0,
            "estimated_count": 0, "human_seconds": None, "record_count": count,
            "unknown_count": count, "excluded_ai_count": 0}


def _totals(count):
    return {**_counts(count), "categories": {
        category: _counts(count if category == "unknown" else 0) for category in (*CATEGORIES, "unknown")}}


def heatmap(store, month, scope="month"):
    try:
        first, following = period_bounds(month, scope)
    except ValueError as exc:
        raise BridgeError(str(exc)) from None
    left = int(datetime.combine(first, time(), LOCAL_TZ).timestamp() * 1000)
    right = int(datetime.combine(following, time(), LOCAL_TZ).timestamp() * 1000)
    timestamp = now_ms()
    with store.transaction():
        blocks = store.current_activities()
        rows = store.db.execute("""SELECT strftime('%Y-%m-%d',reported_at_ms/1000,'unixepoch','+8 hours') AS day,
                                   count(*) AS count FROM desktop_submissions
                                   WHERE reported_at_ms>=? AND reported_at_ms<? GROUP BY day""", (left, right)).fetchall()
        counts = {row["day"]: row["count"] for row in rows}
        submissions = store.db.execute("SELECT observation_id,reported_at_ms FROM desktop_submissions WHERE reported_at_ms>=? AND reported_at_ms<?", (left, right)).fetchall() if blocks else []
    covered = {ref for block in blocks for ref in block.get("evidence_refs", [])}
    raw_counts = {}
    for row in submissions:
        if row["observation_id"] not in covered:
            key = datetime.fromtimestamp(row["reported_at_ms"] / 1000, LOCAL_TZ).date().isoformat()
            raw_counts[key] = raw_counts.get(key, 0) + 1
    projected = [item for block in blocks if (item := _organized_item(store, block, left, right)) is not None]
    def totals(start, finish, raw_count):
        from .heatmap import _day_items, _totals as activity_totals
        from .timeline import _clip
        selected = _day_items(projected, start, finish)
        # Heatmap treats approximate windows separately from definite ones.
        selected = [{**item, "recorded_intervals": [] if item.get("estimated") else _clip(item["recorded_intervals"], start, finish)} for item in selected]
        block_map = {block["activity_id"]: block for block in blocks}
        def category_totals(items, raw):
            result = activity_totals(items, start, finish, block_map)
            result["record_count"] += raw
            result["unknown_count"] += raw
            return result
        return {**category_totals(selected, raw_count), "categories": {
            category: category_totals([item for item in selected if (item.get("category") or "unknown") == category], raw_count if category == "unknown" else 0)
            for category in (*CATEGORIES, "unknown")}}
    days, current = [], first
    while current < following:
        day = current.isoformat()
        start = int(datetime.combine(current, time(), LOCAL_TZ).timestamp() * 1000)
        values = totals(start, start + 86400000, raw_counts.get(day, 0)) if blocks else _totals(counts.get(day, 0))
        days.append({"date": day, "future": start >= timestamp, **values})
        current += timedelta(days=1)
    return {"mode": "desktop_organized" if projected else "desktop_only", "month": month, "scope": scope,
            "range_start": first.isoformat(), "range_end": following.isoformat(), "timezone": "Asia/Taipei",
            "as_of_ms": timestamp, "server_now_ms": timestamp, "days": days,
            "summary": totals(left, right, sum(raw_counts.values())) if blocks else _totals(sum(counts.values())), "data_revision": digest({"scope": store.scope, "days": days}),
            "completeness": "partial" if projected else "raw_only", "source_snapshot_atomic": True,
            "cache": {"state": "fresh", "as_of_ms": timestamp},
            "limitations": ["按需整理的已有区间计热度，估算单列；未整理原文只计条数。", "起止不全不生成工时热度；空白不代表投入为零。"]}
