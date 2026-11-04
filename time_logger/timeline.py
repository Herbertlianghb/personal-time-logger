"""Read-only, versioned desktop timeline over the existing formal time table.

Base start/end columns describe recorded windows.  They do not, by themselves,
prove active human effort.  Only an unchanged, previously verified PTL mapping
can supply that stronger claim.  The desktop never writes historical records.
"""
from __future__ import annotations

import base64
import copy
import json
import re
from datetime import date, datetime, time, timedelta

from .base_adapter import ALLOWLIST, HttpBaseTransport, _canonical, _schema_fields
from .calculator import LOCAL_TZ, interval_union, seconds
from .formal import get_link, record_fingerprint
from .store import digest, now_ms, packed
from .day_window import day_bounds


CACHE_TTL_MS = 60000
TIMELINE_DISPLAY_VERSION = 3
_MARKER = re.compile(r"^\[PTL:([A-Za-z0-9_-]{1,100}):([^\]\n]{1,100})\]\n")
_UNCERTAIN_LEGACY_TIME = re.compile(r"估算|近似|非精确|不确定|起[止始].{0,8}未知|结束.{0,8}未知|切换.{0,8}未知|报告时[刻间]|消息时[刻间]|大约|约\s*\d+\s*分")


def _bounds(day, day_start_hour=0):
    return day_bounds(day, day_start_hour)


def _clip(intervals, left, right):
    return interval_union([[max(a, left), min(b, right)] for a, b in intervals
                           if type(a) is int and type(b) is int and a < b and max(a, left) < min(b, right)])


def _option(value):
    return value[0] if isinstance(value, list) and len(value) == 1 else value if isinstance(value, str) else None


def _timestamp(value):
    result = _canonical(value, "datetime")
    return result if type(result) is int and 946684800000 <= result <= 4102444800000 else None


def _claim_time(block, key):
    claim = block.get(key, {})
    return _timestamp(claim.get("value_ms")) if claim.get("kind") == "explicit_user" else None


def _claims(fields):
    note = fields.get("备注") or ""
    if not isinstance(note, str):
        return None, None
    marker = _MARKER.match(note)
    if not marker:
        return None, None
    try:
        body, _ = json.JSONDecoder().raw_decode(note[marker.end():])
    except ValueError:
        return marker[1], None
    if not isinstance(body, dict) or body.get("activity_id", marker[1]) != marker[1]:
        return marker[1], None
    return marker[1], body


def _records(data):
    names, ids, values = data.get("fields", []), data.get("record_id_list", []), data.get("data", [])
    if not isinstance(names, list) or len(ids) != len(values):
        raise ValueError("invalid_timeline_record_matrix")
    if any(not isinstance(row, list) or len(row) != len(names) for row in values):
        raise ValueError("invalid_timeline_record_values")
    return [{"record_id": rid, "fields": dict(zip(names, row))} for rid, row in zip(ids, values)]


def _query(transport, predicate, field_names):
    """Complete an exact API result; an incomplete page is never cached."""
    selected, offset, seen = [], 0, set()
    while True:
        args = ["--limit", "200", "--offset", str(offset), "--filter-json", packed(predicate)]
        for name in field_names:
            args.extend(["--field-id", name])
        data = transport._call("+record-list", *args)
        rows = _records(data)
        selected.extend(rows)
        if not data.get("has_more"):
            return selected
        next_offset = data.get("next_offset")
        # Native Base v3 uses an offset and has_more without returning the
        # CLI wrapper's optional next_offset. Advance by actual matrix rows;
        # an empty or explicitly non-advancing page still fails closed.
        if next_offset is None and rows:
            next_offset = offset + len(rows)
        if type(next_offset) is not int or next_offset <= offset or next_offset in seen:
            raise ValueError("incomplete_timeline_pagination")
        seen.add(next_offset)
        offset = next_offset
        if len(selected) > 20000:
            raise ValueError("timeline_day_query_too_large")


def _evidence(store, refs):
    return store.evidence(refs) if refs else []


def _report_ms(evidence):
    # The platform report timestamp remains separate from receipt/processing.
    # A desktop timestamp flagged untrusted is not used to place a precise node.
    values = [e["created_at_ms"] for e in evidence
              if type(e.get("created_at_ms")) is int
              and not (e.get("source") == "desktop" and
                       e.get("desktop_context", {}).get("report_time_trust") != "near_server_clock")]
    return max(values) if values else None


def _received_ms(evidence):
    values = [e.get("desktop_context", {}).get("server_received_at_ms", e.get("observed_at_ms")) for e in evidence]
    values = [value for value in values if type(value) is int]
    return max(values) if values else None


def _date_anchor(evidence):
    reported = _report_ms(evidence)
    return reported if reported is not None else _received_ms(evidence)


def _sleep_identity(block):
    return (block.get("actor") == "human" and block.get("role") == "primary"
            and bool(re.search(r"睡觉|睡眠|入睡|就寝", block.get("name", ""))))


def _current_report(source, as_of_ms):
    return (source.get("id") == source.get("current_observation_id")
            and source.get("kind") in {"receive", "edit"} and source.get("message_type") == "text"
            and not source.get("is_acceptance_test") and _timestamp(source.get("created_at_ms")) is not None
            and source["created_at_ms"] <= as_of_ms
            and not (source.get("source") == "desktop" and
                     source.get("desktop_context", {}).get("report_time_trust") != "near_server_clock"))


def _sleep_report_pair(block, evidence, as_of_ms):
    """A narrow display estimate from an already accepted sleep/wake association."""
    if not _sleep_identity(block) or block.get("status") != "ended" or block.get("_acceptance_test"):
        return None
    if any(block.get(key, {}).get("kind") == "explicit_user" for key in ("start", "end")):
        return None
    sources = {e["id"]: e for e in evidence}
    refs = block.get("evidence_refs", [])
    if not refs or any(ref not in sources or not _current_report(sources[ref], as_of_ms) for ref in refs):
        return None
    pair = []
    for key, role in (("start", "sleep_report"), ("end", "wake_report")):
        claim = block.get(key, {})
        source = sources.get(claim.get("source_id"))
        if not source or source["id"] not in refs or not claim.get("quote") or claim["quote"] not in (source.get("content") or ""):
            return None
        text = (source.get("content") or "").strip()
        # Match a direct first-person present report, not a substring stripped
        # out of a plan, denial, historical account, question or quotation.
        if re.search(r'[?？“”"「」『』]|(?:没|不|未)(?:有)?(?:睡|醒)|睡不', text):
            return None
        first = re.split(r"[，,。！!；;\n]", text, maxsplit=1)[0].strip()
        cue = r"(?:去)?(?:睡觉|睡了|入睡|就寝)" if key == "start" else r"(?:睡醒|醒来|醒|起床)"
        if not re.fullmatch(r"(?:我)?(?:现在|刚刚|刚)?" + cue + r"(?:了|啦|咯|啊)?", first):
            return None
        pair.append({"type": "evidence", "id": source["id"], "report_ms": source["created_at_ms"], "role": role})
    # Reject very old/mistaken associations conservatively; this is a display
    # bound, not a physiological claim about possible sleep duration.
    if pair[0]["id"] == pair[1]["id"] or not 0 < pair[1]["report_ms"] - pair[0]["report_ms"] <= 48 * 3600000:
        return None
    return {"start_ms": pair[0]["report_ms"], "end_ms": pair[1]["report_ms"],
            "kind": "sleep_report_window", "label": "睡眠 · 依据报告估计",
            "uncertainty": "仅以睡前与醒来报告定位，非实测入睡/醒来瞬间，不计主动人工投入。", "source_refs": pair}


def _sleep_conflict(window, activity_id, items, blocks, store, as_of_ms):
    start, end = window["source_refs"][0]["report_ms"], window["source_refs"][1]["report_ms"]
    for item in items:
        if item.get("activity_id") == activity_id or item.get("actor") == "ai" or item.get("source") == "backend_accepted_ai" or item.get("status") in {"recalled", "cancelled", "superseded"}:
            continue
        if re.search(r"睡觉|睡眠|入睡|就寝", item.get("name", "")):
            continue
        intervals = item.get("recorded_intervals", []) + item.get("human_intervals", [])
        a, b = item.get("start_ms"), item.get("end_ms")
        if type(a) is int and type(b) is int:
            intervals = intervals + [[a, b]]
        if any(max(start, a) < min(end, b) for a, b in intervals):
            return True
    for block in blocks.values():
        if (block.get("activity_id") == activity_id or _sleep_identity(block) or block.get("_acceptance_test")
                or block.get("actor") != "human" or block.get("role") != "primary" or block.get("status") in {"recalled", "cancelled"}):
            continue
        sources = {e["id"]: e for e in _evidence(store, block.get("evidence_refs", []))}
        intervals = [(piece.get("start_ms"), piece.get("end_ms")) for piece in block.get("human_intervals", [])]
        a, b = _claim_time(block, "start"), _claim_time(block, "end")
        if a is not None and b is not None:
            intervals.append((a, b))
        if any(type(a) is int and type(b) is int and max(start, a) < min(end, b) for a, b in intervals):
            return True
        for key in ("start", "end"):
            claim = block.get(key, {})
            value = claim.get("value_ms")
            source = sources.get(claim.get("source_id"), {})
            if claim.get("kind") == "explicit_user" and type(value) is int and start < value < end:
                return True
            if not _current_report(source, as_of_ms):
                continue
            if claim.get("kind") == "report_anchor" and value == source["created_at_ms"] and start < value < end:
                return True
            # Only a new current-activity cue may place an unknown boundary.
            # Later grouping answers and retrospective reports are not events.
            if (claim.get("kind") == "unknown" and key == "start" and start < source["created_at_ms"] < end
                    and claim.get("quote") and claim["quote"] in (source.get("content") or "")
                    and re.match(r"^(?:我)?(?:现在|正在|开始)", source.get("content", ""))):
                return True
    return False


def _relevant(start, end, reported, left, right):
    if start is not None and end is not None and start < end:
        return max(start, left) < min(end, right)
    if start is not None:
        return left <= start < right
    if end is not None:
        return left <= end < right
    return reported is not None and left <= reported < right


def _same_baseline(fields, baseline, schema):
    return bool(baseline) and all(
        name in schema and _canonical(fields.get(name), schema[name]["type"]) == _canonical(value, schema[name]["type"])
        for name, value in baseline.items())


def _local_state(store):
    with store.lock:
        mappings = [dict(r) for r in store.db.execute("SELECT * FROM base_mapping WHERE remote_id IS NOT NULL")]
        blocks = store.current_activities()
    return {b["activity_id"]: b for b in blocks}, mappings


def _item(fields, record_id, mapping, blocks, schema, store, left, right, as_of_ms):
    aid, claims = _claims(fields)
    claims = claims or {}
    link = get_link(store, mapping["activity_id"]) if mapping else None
    is_legacy = bool(link and link["record_id"] == record_id)
    if is_legacy:
        aid = mapping["activity_id"]
    candidate = blocks.get(aid)
    baseline = json.loads(mapping["baseline"]) if mapping and mapping.get("baseline") else {}
    unchanged = bool(mapping and _same_baseline(fields, baseline, schema))
    verified = bool(unchanged and candidate and candidate["revision"] == mapping["last_revision"]
                    and (is_legacy or claims.get("revision") == candidate["revision"]))
    block = candidate if verified else claims if unchanged else {}
    evidence = _evidence(store, block.get("evidence_refs", []))
    if block.get("_acceptance_test") or any(e.get("is_acceptance_test") for e in evidence):
        return None
    if claims.get("status") == "superseded" and unchanged:
        return None
    start, end = _timestamp(fields.get("开始时间")), _timestamp(fields.get("结束时间"))
    report_ms = _report_ms(evidence)
    received_ms = _received_ms(evidence)
    # An unchanged PTL fact can retain explicit real boundaries in its claims
    # without writing them into Base's effort interval columns.
    explicit_start = _claim_time(block, "start") if verified else None
    explicit_end = _claim_time(block, "end") if verified else None
    uncertain_note = (not verified or is_legacy) and bool(_UNCERTAIN_LEGACY_TIME.search(str(fields.get("备注") or "")))
    estimated_legacy = uncertain_note and not (explicit_start is not None and explicit_end is not None)
    reported_window = verified and any(block.get(key, {}).get("kind") in {"estimate", "report_anchor"}
                                       for key in ("start", "end"))
    # A correction can make one boundary explicit without resolving the other.
    # New evidence wins for that field; the old estimated note must neither
    # erase it nor promote the remaining approximate endpoint to an exact one.
    display_start = explicit_start if explicit_start is not None else None if uncertain_note else start
    display_end = explicit_end if explicit_end is not None else None if uncertain_note else end
    estimate = _sleep_report_pair(block, evidence, as_of_ms) if verified and not is_legacy and start is None and end is None else None
    estimated = _clip([[estimate["start_ms"], estimate["end_ms"]]], left, right) if estimate else []
    if not _relevant(start or display_start, end or display_end, _date_anchor(evidence), left, right) and not estimated:
        return None
    recorded = _clip([[start, end]], left, right) if start is not None and end is not None and not estimated_legacy else []
    status = block.get("status", "ended" if start is not None and end is not None else "unknown")
    excluded = status in {"recalled", "cancelled", "superseded"}
    # A verified Base mapping proves that the row is unchanged, not that all
    # of its occupied window was active human effort. Intersect actual saved
    # effort evidence with the formal row segments; a legacy seed has none.
    evidence_intervals = [(piece.get("start_ms"), piece.get("end_ms")) for piece in block.get("human_intervals", [])]
    human = _clip([[max(a, c), min(b, d)] for a, b in recorded for c, d in evidence_intervals
                   if type(c) is int and type(d) is int and max(a, c) < min(b, d)], left, right) if verified and block.get("actor") == "human" and block.get("role") == "primary" and not excluded else []
    duration = seconds(recorded) if recorded else None
    scalar = block.get("reported_duration_seconds")
    if type(scalar) not in (int, float) or scalar < 0:
        scalar = None
    if estimated_legacy and start is not None and end is not None and end >= start and explicit_start is None and explicit_end is None:
        scalar = (end - start) / 1000
    return {"id": "activity:" + aid + ":" + str(block.get("revision", mapping["last_revision"] if mapping else 0)) if aid and unchanged else "base:" + record_id,
            "record_ids": [record_id], "activity_id": aid if unchanged else None,
            "record_id": record_id, "record_fingerprint": record_fingerprint(record_id, fields, schema),
            "expected_fingerprint": record_fingerprint(record_id, fields, schema),
            "revision": block.get("revision", mapping["last_revision"] if mapping else None),
            "name": str(fields.get("时间花销") or "未命名活动"),
            "start_ms": display_start, "end_ms": display_end, "report_ms": report_ms,
            "start_kind": block.get("start", {}).get("kind") if verified else None,
            "end_kind": block.get("end", {}).get("kind") if verified else None,
            "received_ms": received_ms,
            "original_start_ms": start, "original_end_ms": end,
            "recorded_intervals": [] if excluded else recorded,
            "estimated_intervals": [{**estimate, "start_ms": a, "end_ms": b} for a, b in estimated],
            "human_intervals": human, "ai_intervals": [],
            "duration_seconds": duration, "duration_kind": "confirmed_human" if human else "recorded_window" if recorded else "estimated_window" if estimated_legacy else "reported_scalar" if scalar is not None else "unknown",
            "reported_duration_seconds": scalar, "estimated": estimated_legacy or reported_window or bool(scalar is not None and not recorded),
            "role": block.get("role", "primary"), "actor": "human" if human else "unknown",
            "status": status, "category": _option(fields.get("一级分类")), "project": _option(fields.get("二级项目")),
            "source": "feishu_base", "source_refs": [{"type": "feishu_record", "id": record_id}]
                + [{"type": "evidence", "id": e["id"], "report_ms": e.get("created_at_ms"), "received_ms": e.get("observed_at_ms")} for e in evidence],
            "result": fields.get("结果记录") or "", "subtasks": copy.deepcopy(block.get("subtasks", [])) if unchanged else [],
            "notes": fields.get("备注") or "",
            "uncertainty": block.get("uncertainty", "") if unchanged else "正式表起止为记录区间；没有单独的主动投入依据。",
            "formal_state": "linked_legacy" if verified and is_legacy else "verified_ptl" if verified else "remote_modified" if mapping and not unchanged else "formal_base",
            "parent_activity_id": block.get("parent_activity_id") if verified else None}


def _merge_items(items):
    grouped = {}
    for item in items:
        key = item["id"]
        if key not in grouped:
            grouped[key] = item
            continue
        previous = grouped[key]
        previous["record_ids"] = sorted(set(previous["record_ids"] + item["record_ids"]))
        # Corrections to a grouped PTL activity use activity_id/revision.
        # A single-row fingerprint must never masquerade as the whole group.
        previous["record_id"] = None
        previous["record_fingerprint"] = None
        previous["expected_fingerprint"] = None
        previous["source_refs"] += [ref for ref in item["source_refs"] if ref not in previous["source_refs"]]
        for name in ("recorded_intervals", "human_intervals", "ai_intervals"):
            previous[name] = interval_union(previous[name] + item[name])
        previous["duration_seconds"] = seconds(previous["recorded_intervals"]) if previous["recorded_intervals"] else None
    return list(grouped.values())


def _ai_items(store, blocks, left, right):
    result = []
    for block in blocks.values():
        if block.get("actor") != "ai" or block.get("role") != "primary" or block.get("status") in {"recalled", "cancelled"}:
            continue
        evidence = _evidence(store, block.get("evidence_refs", []))
        if block.get("_acceptance_test") or any(e.get("is_acceptance_test") for e in evidence):
            continue
        start, end, reported = _claim_time(block, "start"), _claim_time(block, "end"), _report_ms(evidence)
        if not _relevant(start, end, _date_anchor(evidence), left, right):
            continue
        intervals = _clip([[start, end]], left, right) if start is not None and end is not None else []
        result.append({"id": "ai:" + block["activity_id"], "record_ids": [], "activity_id": block["activity_id"], "revision": block["revision"],
                       "name": block.get("name", "AI后台"), "start_ms": start, "end_ms": end, "report_ms": reported,
                       "received_ms": _received_ms(evidence),
                       "recorded_intervals": [], "human_intervals": [], "ai_intervals": intervals,
                       "duration_seconds": seconds(intervals) if intervals else None, "duration_kind": "ai_background" if intervals else "unknown",
                       "reported_duration_seconds": block.get("reported_duration_seconds"), "estimated": False,
                       "role": "primary", "actor": "ai", "status": block.get("status", "unknown"),
                       "category": block.get("category"), "project": block.get("project"), "source": "backend_accepted_ai",
                       "source_refs": [{"type": "evidence", "id": e["id"], "report_ms": e.get("created_at_ms")} for e in evidence],
                       "result": block.get("result", ""), "subtasks": copy.deepcopy(block.get("subtasks", [])),
                       "uncertainty": block.get("uncertainty", ""), "formal_state": "accepted_ai_not_base",
                       "parent_activity_id": block.get("parent_activity_id")})
    return result


def _summary(items):
    active = [item for item in items if item.get("role") != "detail" and item.get("status") not in {"recalled", "cancelled", "superseded"}]
    humans = [piece for item in active for piece in item["human_intervals"]]
    recorded = [piece for item in active for piece in item["recorded_intervals"]]
    ais = [piece for item in active for piece in item["ai_intervals"]]
    scalar = [item["reported_duration_seconds"] for item in active if not item["human_intervals"] and not item["recorded_intervals"]
              and type(item.get("reported_duration_seconds")) in (int, float)]
    def human_category(categories):
        values = [piece for item in active if item["category"] in categories for piece in item["human_intervals"]]
        return seconds(values) if values else None
    return {"human_seconds": seconds(humans) if humans else None,
            "work_study_seconds": human_category({"工作", "学习"}),
            "other_seconds": human_category({item["category"] for item in active} - {"工作", "学习"}),
            "recorded_seconds": seconds(recorded) if recorded else None,
            "work_study_recorded_seconds": seconds([piece for item in active if item["category"] in {"工作", "学习"} for piece in item["recorded_intervals"]]) if recorded else None,
            "ai_seconds": seconds(ais) if ais else None,
            "scalar_seconds": sum(scalar) if scalar else None,
            "estimated_seconds": sum(scalar) if scalar else None,
            "scalar_is_additive": False, "unknown_count": sum(item["duration_seconds"] is None and item.get("reported_duration_seconds") is None for item in active),
            "formal_record_count": sum(bool(item["record_ids"]) for item in active),
            "completeness": "partial", "scope": "已记录区间与有依据的人工分别计算并集；标量时长、父子任务及AI后台不加到人工。"}


def _raw_reports(store, left, right, represented):
    """Current unrepresented messages are report anchors, never activity windows."""
    with store.lock:
        rows = store.db.execute("""SELECT o.observation_id FROM observations o
            JOIN messages m USING(message_key)
            WHERE m.app_scope=? AND o.observation_id=m.current_observation_id
            AND m.message_type='text' AND o.kind IN ('receive','edit')
            AND m.message_created_at_ms>=? AND m.message_created_at_ms<?
            AND NOT EXISTS(SELECT 1 FROM acceptance_tests t
                WHERE t.app_scope=m.app_scope AND t.message_id=m.message_id)
            ORDER BY m.message_created_at_ms,o.observation_id LIMIT 20001""",
            (store.scope, left, right)).fetchall()
    if len(rows) > 20000:
        raise ValueError("timeline_report_bound")
    ids = [r[0] for r in rows if r[0] not in represented]
    evidence = store.evidence(ids) if ids else []
    result = []
    for entry in evidence:
        text = entry.get("content") or ""
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
                text = decoded["text"]
        except ValueError:
            pass
        if not text.strip() or entry.get("is_acceptance_test"):
            continue
        reported, received = _report_ms([entry]), _received_ms([entry])
        result.append({"id": "raw:" + entry["id"], "observation_id": entry["id"],
            "name": text, "record_ids": [], "record_id": None, "activity_id": None,
            "revision": None, "editable": False, "start_ms": None, "end_ms": None,
            "report_ms": reported, "received_ms": received,
            "original_start_ms": None, "original_end_ms": None,
            "recorded_intervals": [], "human_intervals": [], "ai_intervals": [], "estimated_intervals": [],
            "duration_seconds": None, "reported_duration_seconds": None, "duration_kind": "unknown",
            "estimated": False, "role": "primary", "actor": "unknown", "status": "unknown",
            "category": None, "project": None,
            "source": "desktop_raw" if entry.get("source") == "desktop" else "feishu_raw",
            "formal_state": "pending_organization", "result": "", "subtasks": [], "notes": "",
            "source_refs": [{"type": "evidence", "id": entry["id"], "report_ms": reported}],
            "uncertainty": "已保存原文，待整理；报告时刻不代表活动起止或时长。"})
    return result


class TimelineReader:
    def __init__(self, store, transport, *, cache_ttl_ms=CACHE_TTL_MS, clock=now_ms):
        self.store, self.transport, self.cache_ttl_ms, self.clock = store, transport, cache_ttl_ms, clock

    def _fetch(self, day, timestamp, local_state, day_start_hour=0):
        left, day_right = _bounds(day, day_start_hour)
        right = min(day_right, timestamp)
        schema = _schema_fields(self.transport.field_schema())
        if any(schema.get(name, {}).get("type") != kind for name, kind in
               (("时间花销", "text"), ("开始时间", "datetime"), ("结束时间", "datetime"), ("备注", "text"))):
            raise ValueError("timeline_schema_mismatch")
        projected = [name for name in ALLOWLIST if name in schema]
        blocks, mappings = local_state
        mapping_by_id = {mapping["remote_id"]: mapping for mapping in mappings}
        found = {}
        if right > left:
            selected_date = date.fromisoformat(day)
            # Existing verified Base v3 behavior: use broad day predicates and
            # compare exact milliseconds locally. This includes activities
            # beginning before the selected day without scanning all history.
            dates = [selected_date] + ([selected_date + timedelta(days=1)] if day_start_hour else [])
            predicates = [{"logic": "and", "conditions": [[field, "==", f"ExactDate({chosen.isoformat()})"]]}
                          for chosen in dates for field in ("开始时间", "结束时间")]
            predicates.append({"logic": "and", "conditions": [
                ["开始时间", "<", f"ExactDate({(dates[-1] + timedelta(days=1)).isoformat()})"],
                ["结束时间", ">", f"ExactDate({(selected_date - timedelta(days=1)).isoformat()})"]]})
            for predicate in predicates:
                for row in _query(self.transport, predicate, projected):
                    if row["record_id"] in found and found[row["record_id"]]["fields"] != row["fields"]:
                        raise ValueError("timeline_remote_changed_during_read")
                    found[row["record_id"]] = row
            # Untimed PTL facts have no Base date field. Retrieve only mapped
            # facts whose saved source report belongs to this date.
            for mapping in mappings:
                baseline = json.loads(mapping["baseline"]) if mapping.get("baseline") else {}
                if _timestamp(baseline.get("开始时间")) is not None or _timestamp(baseline.get("结束时间")) is not None:
                    continue
                _, claims = _claims(baseline)
                evidence = _evidence(self.store, (claims or {}).get("evidence_refs", []))
                candidate = blocks.get(mapping["activity_id"], {})
                pair = _sleep_report_pair(candidate, evidence, timestamp) if candidate.get("revision") == mapping["last_revision"] else None
                sleep_overlap = pair and max(left, pair["start_ms"]) < min(right, pair["end_ms"])
                if (_relevant(None, None, _date_anchor(evidence), left, right) or sleep_overlap) and mapping["remote_id"] not in found:
                    row = self.transport.get(mapping["remote_id"])
                    if row:
                        found[row["record_id"]] = row
        from .heatmap import CATEGORIES, _heatmap_item, _merge_heatmap_items, _totals
        items, heat_items = [], []
        for row in found.values():
            item = _item(row["fields"], row["record_id"], mapping_by_id.get(row["record_id"]), blocks, schema, self.store, left, right, timestamp)
            if item:
                items.append(item)
                heat_items.append(_heatmap_item(copy.deepcopy(item), row["fields"], left, right))
        items = _merge_items(items) + _ai_items(self.store, blocks, left, right)
        heat_items = _merge_heatmap_items(heat_items)
        recorded_totals = _totals(heat_items, left, right, blocks)
        recorded_totals["categories"] = {category: _totals(
            [item for item in heat_items if item.get("category") == category], left, right, blocks)
            for category in CATEGORIES}
        for item in items:
            item["estimated_intervals"] = [window for window in item.get("estimated_intervals", [])
                if not _sleep_conflict(window, item.get("activity_id"), items, blocks, self.store, timestamp)]
        if day_start_hour:
            represented = {ref["id"] for item in items for ref in item.get("source_refs", []) if ref.get("type") == "evidence"}
            items += _raw_reports(self.store, left, right, represented)
        items.sort(key=lambda item: (item["start_ms"] if item["start_ms"] is not None else item["report_ms"] if item["report_ms"] is not None else day_right, item["id"]))
        raw_hash = digest(sorted((rid, row["fields"]) for rid, row in found.items()))
        return {"date": day, "day_start_hour": day_start_hour, "timezone": "Asia/Taipei", "day_start_ms": left, "day_end_ms": day_right,
                "as_of_ms": timestamp, "server_now_ms": timestamp, "display_end_ms": max(left, right),
                "data_revision": digest({"records": raw_hash, "items": items}), "source_hash": raw_hash,
                "items": items, "summary": _summary(items), "recorded_totals": recorded_totals, "completeness": "partial",
                "source_snapshot_atomic": False,
                "consistency": "完整有界读取的同一组数据计算明细与汇总；Base接口不提供跨页原子快照。重复记录若中途变化则保留旧缓存。",
                "limitations": ["无日期且没有已保存报告来源的旧记录无法归入某一天。", "正式表记录区间不自动等于有效人工投入。", "AI后台为已整理证据，单列且不写作人工Base工时。", "Base分页没有原子快照版本；无法排除读取期间未观察到的并发增删。"]}

    def history(self, day, cursor=None, limit=100, *, refresh=False, day_start_hour=0):
        left, right = _bounds(day, day_start_hour)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("timeline_page_limit")
        if type(refresh) is not bool:
            raise ValueError("timeline_refresh_boolean")
        timestamp = self.clock()
        local_state = _local_state(self.store)
        sleep_refs = {ref for block in local_state[0].values() if _sleep_identity(block) for ref in block.get("evidence_refs", [])}
        state_hash = digest({"display_version": TIMELINE_DISPLAY_VERSION, "mappings": local_state[1],
            "activities": [(key, value["revision"]) for key, value in sorted(local_state[0].items())],
            "reports": self._report_revision(left, right) if day_start_hour else None,
            "sleep_sources": [(e["id"], e["current_observation_id"]) for e in _evidence(self.store, sorted(sleep_refs))]})
        source = getattr(self.transport, "table_key", None)
        cache_key = "desktop_timeline_cache:" + (digest(source)[:24] + ":" if source is not None else "") + day
        if day_start_hour:
            cache_key += ":start:" + str(day_start_hour)
        cached = self.store.get_setting(cache_key)
        cache_state, error = "fresh", None
        offset = 0
        if cursor:
            try:
                token = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                offset = token["offset"]
                if (not cached or token["revision"] != cached["data_revision"] or token["date"] != day
                        or token.get("day_start_hour", 0) != day_start_hour or type(offset) is not int or offset < 0):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ValueError("timeline_cursor_expired") from None
            snapshot = cached
            cache_state = "cached"
        elif (cached and cached.get("local_state_hash") == state_hash
              and 0 <= timestamp - cached["as_of_ms"] < self.cache_ttl_ms):
            snapshot = cached
            cache_state = "cached"
        else:
            try:
                snapshot = self._fetch(day, timestamp, local_state, day_start_hour)
                snapshot["local_state_hash"] = state_hash
                # One setting write commits the complete snapshot and its totals.
                self.store.set_setting(cache_key, snapshot)
            except Exception as exc:
                if not cached:
                    raise
                snapshot = cached
                cache_state, error = "stale", type(exc).__name__
        # Cached formal data may remain useful during an outage. An estimate
        # must nevertheless lose its display as soon as a local source is
        # recalled, superseded, or contradicted, including on cursor requests.
        result = copy.deepcopy(snapshot)
        removed = False
        for item in result["items"]:
            windows = item.get("estimated_intervals", [])
            if not windows:
                continue
            block = local_state[0].get(item.get("activity_id"), {})
            pair = _sleep_report_pair(block, _evidence(self.store, block.get("evidence_refs", [])), timestamp)
            safe = [window for window in windows if pair and block.get("revision") == item.get("revision")
                    and window.get("source_refs") == pair["source_refs"]
                    and not _sleep_conflict(window, item.get("activity_id"), result["items"], local_state[0], self.store, timestamp)]
            item["estimated_intervals"] = safe
            removed = removed or len(safe) != len(windows)
        if removed:
            result["data_revision"] = digest({"records": result["source_hash"], "items": result["items"]})
            self.store.set_setting(cache_key, result)
            if cursor:
                raise ValueError("timeline_cursor_expired")
        total = len(result["items"])
        result["items"] = result["items"][offset:offset + limit]
        result["activities"] = result["items"]
        result["total_items"] = total
        result["server_now_ms"] = timestamp
        result["cache"] = {"state": cache_state, "as_of_ms": snapshot["as_of_ms"], "refresh_error": error,
                           "min_refresh_interval_ms": self.cache_ttl_ms}
        result["next_cursor"] = base64.urlsafe_b64encode(packed({"date": day, "day_start_hour": day_start_hour, "revision": result["data_revision"], "offset": offset + limit}).encode("utf-8")).decode("ascii") if offset + limit < total else None
        return result

    def _report_revision(self, left, right):
        with self.store.lock:
            return [tuple(row) for row in self.store.db.execute("""SELECT m.message_key,m.current_observation_id,o.kind
                FROM messages m JOIN observations o ON o.observation_id=m.current_observation_id
                WHERE m.app_scope=? AND m.message_type='text'
                AND m.message_created_at_ms>=? AND m.message_created_at_ms<? ORDER BY m.message_key""",
                (self.store.scope, left, right))]


def timeline_history(store, config, day, cursor=None, limit=100, *, refresh=False, day_start_hour=0):
    from .feishu import Feishu
    base = config.get("base_token")
    table = config.get("time_table_id") or config.get("table_id")
    # The device is authorized for the server's explicitly configured source;
    # no author's Base is a fallback for someone else's installation.
    if not isinstance(base, str) or not base or not isinstance(table, str) or not table:
        raise ValueError("desktop_formal_source_not_configured")
    platform = Feishu(config["app_id"], config["app_secret"])
    transport = HttpBaseTransport(platform, base, table)
    result = TimelineReader(store, transport).history(day, cursor, limit, refresh=refresh, day_start_hour=day_start_hour)
    if day_start_hour:
        from .daily_goals import DailyGoalReader
        standards = config.get("time_standards_table_id")
        result["daily_goals"] = (DailyGoalReader(store, HttpBaseTransport(platform, base, standards),
            settings_url=f"https://my.feishu.cn/base/{base}?table={standards}").read(day) if standards else
            {"date": day, "source": "feishu", "work_hours": None, "study_hours": None,
             "cache": {"state": "unavailable", "refresh_error": "not_configured"}})
    return result
