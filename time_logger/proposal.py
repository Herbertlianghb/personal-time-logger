from __future__ import annotations

import re
from datetime import datetime, timedelta

from .calculator import LOCAL_TZ, interval_union


class InvalidProposal(ValueError):
    pass


def _chinese_number(text):
    if text.isdigit():
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "十" in text:
        left, right = text.split("十", 1)
        return (digits.get(left, 1) if left else 1) * 10 + (digits.get(right, 0) if right else 0)
    return digits.get(text)


def _explicit_clock_matches(value, quote, source, precision):
    """Check represented times against cited time expressions, not activity keywords."""
    if type(value) is not int or not 946684800000 <= value <= 4102444800000:
        return False
    local = datetime.fromtimestamp(value / 1000, LOCAL_TZ)
    if precision == "minute" and value % 60000:
        return False
    clocks = set()
    def normalized_hour(hour, position):
        periods = re.findall(r"上午|早上|凌晨|下午|晚上|傍晚|中午", quote[:position])
        if periods and periods[-1] in {"下午", "晚上", "傍晚", "中午"} and hour < 12:
            return hour + 12
        if periods and periods[-1] == "凌晨" and hour == 12:
            return 0
        return hour
    for match in re.finditer(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?::([0-5]\d))?(?!\d)", quote):
        hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
        hour = normalized_hour(hour, match.start())
        clocks.add((hour, minute, second))
    # User shorthand such as “853醒来” or “1348上厕所” is a clock expression. Do not
    # reinterpret a year, date component, quantity or money as HHmm.
    for match in re.finditer(r"(?<![\d/.-])([01]?\d|2[0-3])([0-5]\d)(?![\d年月日号元块秒分钟小时个字词次页步人件天/%.-])", quote):
        clocks.add((normalized_hour(int(match[1]), match.start()), int(match[2]), 0))
    pattern = r"([零〇一二两三四五六七八九十\d]{1,3})(?:点|时)(?:半|一刻|三刻|([零〇一二两三四五六七八九十\d]{1,3})分?)?"
    for match in re.finditer(pattern, quote):
        hour = _chinese_number(match[1])
        minute = _chinese_number(match[2]) if match[2] else 30 if "半" in match[0] else 45 if "三刻" in match[0] else 15 if "一刻" in match[0] else 0
        # In “六点一直学习”, 一 belongs to 一直, not to an unstated minute.
        if match[2] and len(match[2]) == 1 and not match[2].isdigit() and not match[0].endswith("分"):
            minute = 0
        if hour is None or minute is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            continue
        hour = normalized_hour(hour, match.start())
        clocks.add((hour, minute, 0))
    if (local.hour, local.minute, local.second) not in clocks:
        return False
    dates = re.findall(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})(?:日|号)?", quote)
    if dates and (local.year, local.month, local.day) not in {(int(y), int(m), int(d)) for y, m, d in dates}:
        return False
    if not dates:
        text = quote if re.search(r"今天|昨天|明天|前天|后天|昨晚", quote) else source.get("content", "")
        cues = re.findall(r"今天|昨天|明天|前天|后天|昨晚", text)
        if cues and source.get("created_at_ms"):
            reported_day = datetime.fromtimestamp(source["created_at_ms"] / 1000, LOCAL_TZ).date()
            offsets = {"今天": 0, "昨天": -1, "昨晚": -1, "明天": 1, "前天": -2, "后天": 2}
            days = {reported_day + timedelta(days=offsets[cue]) for cue in cues}
            if any(h >= 20 for h, _, _ in clocks) and local.hour <= 6:
                days.update(day + timedelta(days=1) for day in tuple(days))
            if local.date() not in days:
                return False
    return True


def validate_proposal(proposal, evidence, current_blocks=()):
    """Validate provenance and arithmetic; semantic accuracy needs separate evaluation."""
    if proposal.get("version") != 1 or not isinstance(proposal.get("blocks"), list) or not isinstance(proposal.get("questions"), list):
        raise InvalidProposal("proposal_schema")
    sources = {e["id"]: e for e in evidence}
    current = {b["activity_id"]: b for b in current_blocks}
    seen = set()
    for block in proposal["blocks"]:
        aid = block.get("activity_id")
        if aid:
            if aid in seen or aid not in current:
                raise InvalidProposal("unknown_or_duplicate_activity_id")
            seen.add(aid)
            if block.get("supersedes_revision") != current[aid]["revision"]:
                raise InvalidProposal("stale_revision")
        elif block.get("supersedes_revision") is not None:
            raise InvalidProposal("new_activity_has_previous_revision")
        if block.get("actor") not in {"human", "ai"} or block.get("role") not in {"primary", "detail"}:
            raise InvalidProposal("actor_or_statistical_role")
        refs = block.get("evidence_refs")
        if not refs or any(ref not in sources for ref in refs):
            raise InvalidProposal("missing_evidence")
        if any(sources[ref]["current_observation_id"] != ref for ref in refs):
            raise InvalidProposal("superseded_observation")
        if any(sources[ref]["kind"] == "recall" for ref in refs) and block.get("status") != "recalled":
            raise InvalidProposal("recalled_evidence_cannot_support_active_fact")
        for field in ("start", "end"):
            claim = block.get(field, {})
            kind = claim.get("kind")
            value = claim.get("value_ms")
            source = sources.get(claim.get("source_id"))
            if kind == "unknown":
                if value is not None:
                    raise InvalidProposal("unknown_has_value")
                continue
            if kind not in {"explicit_user", "report_anchor", "estimate", "formal_record"} or type(value) is not int:
                raise InvalidProposal("invalid_time_claim")
            if not source or source["id"] not in refs:
                raise InvalidProposal("missing_time_source")
            if claim.get("date_resolution") == "ambiguous":
                raise InvalidProposal("ambiguous_date_has_value")
            if kind == "formal_record":
                snapshot = source.get("formal_record", {})
                remote_name = "开始时间" if field == "start" else "结束时间"
                before = current.get(aid, {}).get(field, {})
                if (source.get("source") != "formal_record" or source.get("message_type") != "formal_record"
                        or snapshot.get("fields", {}).get(remote_name) != value):
                    raise InvalidProposal("formal_time_does_not_match_snapshot")
                if before.get("value_ms") != value or before.get("source_id") != source["id"] or before.get("kind") != "formal_record":
                    raise InvalidProposal("formal_claim_must_preserve_current")
            elif kind == "report_anchor":
                if not claim.get("quote") or claim["quote"] not in (source["content"] or ""):
                    raise InvalidProposal("time_quote_not_in_evidence")
                if source.get("source") == "desktop" and source.get("desktop_context", {}).get("report_time_trust") != "near_server_clock":
                    raise InvalidProposal("untrusted_device_report_anchor")
                if value != source["created_at_ms"]:
                    raise InvalidProposal("report_anchor_changed")
            elif not claim.get("quote") or claim["quote"] not in (source["content"] or ""):
                raise InvalidProposal("time_quote_not_in_evidence")
            elif kind == "explicit_user" and not _explicit_clock_matches(value, claim["quote"], source, claim.get("precision")):
                raise InvalidProposal("explicit_time_does_not_match_cited_expression")
            if "basis" in claim:
                _validate_window_basis(claim, block, sources, refs)
        start, end = block["start"]["value_ms"], block["end"]["value_ms"]
        if start is not None and end is not None and end < start:
            raise InvalidProposal("negative_window")
        intervals = block.get("human_intervals", [])
        if intervals and (block["actor"] != "human" or block["role"] != "primary"):
            raise InvalidProposal("non_primary_human_effort")
        for interval in intervals:
            src = sources.get(interval.get("source_id"))
            if src and src.get("source") == "formal_record":
                raise InvalidProposal("formal_snapshot_is_not_human_effort_evidence")
            if not src or src["id"] not in refs or not interval.get("quote") or interval["quote"] not in (src["content"] or ""):
                raise InvalidProposal("effort_without_cited_user_evidence")
            a, b = interval["start_ms"], interval["end_ms"]
            if type(a) is not int or type(b) is not int or b < a:
                raise InvalidProposal("invalid_effort_interval")
            if start is None or end is None or a < start or b > end:
                raise InvalidProposal("effort_outside_activity")
            # Report anchors establish a reporting window, never continuous effort.
            if block["start"]["kind"] != "explicit_user" or block["end"]["kind"] != "explicit_user":
                raise InvalidProposal("effort_requires_explicit_boundaries")
        interval_union([[i["start_ms"], i["end_ms"]] for i in intervals])
        duration = block.get("reported_duration_seconds")
        if duration is not None and (type(duration) is not int or duration < 0):
            raise InvalidProposal("invalid_reported_duration")
        for subtask in block.get("subtasks", []):
            if subtask.get("actor") not in {"human", "ai"} or any(ref not in sources for ref in subtask.get("evidence_refs", [])):
                raise InvalidProposal("invalid_subtask")
    if len(proposal["questions"]) > 2:
        raise InvalidProposal("question_round_budget")
    for question in proposal["questions"]:
        if not question.get("semantic_key") or not question.get("ambiguity") or not question.get("text"):
            raise InvalidProposal("question_requires_specific_ambiguity")
        if not question.get("evidence_refs") or any(ref not in sources for ref in question["evidence_refs"]):
            raise InvalidProposal("question_without_evidence")
    return True


def _validate_window_basis(claim, block, sources, refs):
    """Verify a reported/relative window without promoting it to measured effort."""
    basis = claim.get("basis")
    if (claim.get("kind") != "estimate" or claim.get("precision") != "approximate"
            or claim.get("date_resolution") not in {"explicit", "contextual"}
            or not isinstance(basis, dict) or not re.search(r"估算|报告", block.get("uncertainty", ""))):
        raise InvalidProposal("window_basis_requires_estimate")
    anchor = sources.get(basis.get("anchor_source_id"))
    quote = basis.get("anchor_quote")
    value, offset = basis.get("anchor_value_ms"), basis.get("offset_seconds")
    if (not anchor or anchor["id"] not in refs or anchor.get("kind") == "recall"
            or not quote or quote not in (anchor.get("content") or "")
            or type(value) is not int or type(offset) is not int or abs(offset) > 86400
            or re.search(r"不确定|可能|大概|记不清|[?？]", quote)):
        raise InvalidProposal("invalid_window_anchor")
    kind = basis.get("anchor_kind")
    if kind == "report_anchor":
        if (value != anchor.get("created_at_ms") or anchor.get("source") == "formal_record"
                or anchor.get("source") == "desktop" and anchor.get("desktop_context", {}).get("report_time_trust") != "near_server_clock"):
            raise InvalidProposal("untrusted_window_report_anchor")
    elif kind != "explicit_user" or not _explicit_clock_matches(value, quote, anchor, "minute"):
        raise InvalidProposal("window_explicit_anchor_mismatch")
    if offset:
        durations = []
        for match in re.finditer(r"([零〇一二两三四五六七八九十\d]{1,6})\s*(秒钟?|分钟|小时)", claim.get("quote", "")):
            quantity = _chinese_number(match[1])
            if quantity is not None:
                durations.append(quantity * (3600 if match[2] == "小时" else 60 if match[2] == "分钟" else 1))
        if abs(offset) not in durations:
            raise InvalidProposal("window_offset_without_cited_duration")
    if claim["value_ms"] != value + offset * 1000:
        raise InvalidProposal("window_boundary_arithmetic_mismatch")
