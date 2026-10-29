"""Display statistics over already deduplicated UTC+8 day intervals.

These functions do not read records or estimate unobserved time. Category
values are each category's own union, never a share of an additive total.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math

from .calculator import LOCAL_TZ


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _seconds(value):
    # No positive interval means no measurable heat. Missing/invalid values
    # remain unknown instead of becoming fabricated zero-duration days.
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
        return None
    return value


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _metrics(item, category):
    item = _mapping(item)
    return item if category == "全部" else _mapping(_mapping(item.get("categories")).get(category))


def _bounds(snapshot, dates):
    start, end = _date(snapshot.get("range_start")), _date(snapshot.get("range_end"))
    if start and end and start < end:
        return start, end
    month = snapshot.get("month")
    first = _date(month + "-01") if isinstance(month, str) else None
    if first:
        try:
            following = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
            return first, following
        except ValueError:
            pass
    if dates:
        last = max(dates)
        if last < date.max:
            return min(dates), last + timedelta(days=1)
    return None, None


def _as_of(snapshot, end, dates):
    timestamp = snapshot.get("as_of_ms")
    if type(timestamp) in (int, float) and math.isfinite(timestamp):
        try:
            return datetime.fromtimestamp(timestamp / 1000, LOCAL_TZ).date()
        except (ValueError, OverflowError, OSError):
            pass
    # The snapshot owns the time context. Never make the same saved response
    # produce different statistics merely because the computer's date changes.
    return end - timedelta(days=1) if end else max(dates) if dates else None


def build_statistics(snapshot, category="全部"):
    """Return daily, Monday-based weekly and known cumulative interval values.

    ``range_end`` and each week's ``end`` are exclusive. The daily average
    divides by days with positive known intervals. Streaks mean consecutive
    days with such records, not compliance with a time or productivity goal.
    """
    snapshot = _mapping(snapshot)
    raw_days = snapshot.get("days")
    raw_days = raw_days if isinstance(raw_days, list) else []
    by_date = {}
    for item in raw_days:
        item = _mapping(item)
        day = _date(item.get("date"))
        if day:
            by_date[day] = item  # A repeated date must never inflate a sum.
    start, end = _bounds(snapshot, by_date)
    as_of = _as_of(snapshot, end, by_date)
    daily, running, estimated_running, has_interval = [], 0, 0, False
    cursor = start
    while cursor and end and cursor < end:
        item = by_date.get(cursor, {})
        metrics = _metrics(item, category)
        future = bool(item.get("future")) or bool(as_of and cursor > as_of)
        value = None if future else _seconds(metrics.get("recorded_seconds"))
        estimated = min(value, _seconds(metrics.get("estimated_seconds")) or 0) if value is not None else 0
        estimated_count = 0 if future else _count(metrics.get("estimated_count"))
        if value is not None:
            running += value
            estimated_running += estimated
            has_interval = True
        daily.append({"date": cursor.isoformat(), "seconds": value,
                      "estimated_seconds": estimated, "estimated_count": estimated_count,
                      "record_count": 0 if future else _count(metrics.get("record_count")),
                      "unknown_count": 0 if future else _count(metrics.get("unknown_count")),
                      "future": future, "has_interval": value is not None,
                      "cumulative_seconds": running if has_interval and not future else None,
                      "cumulative_estimated_seconds": estimated_running if not future else None})
        cursor += timedelta(days=1)

    elapsed = [day for day in daily if not day["future"]]
    measured = [day for day in elapsed if day["has_interval"]]
    total = sum(day["seconds"] for day in measured) if measured else None
    peak = max((day["seconds"] for day in measured), default=None)
    peak_dates = [day["date"] for day in measured if day["seconds"] == peak]
    longest = streak = 0
    for day in elapsed:
        streak = streak + 1 if day["has_interval"] else 0
        longest = max(longest, streak)

    current, streak_end = 0, None
    if start and end and as_of:
        anchor = min(as_of, end - timedelta(days=1))
        measured_dates = {_date(day["date"]) for day in measured}
        if anchor not in measured_dates:
            anchor -= timedelta(days=1)
        if anchor in measured_dates:
            streak_end = anchor.isoformat()
            while anchor >= start and anchor in measured_dates:
                current += 1
                anchor -= timedelta(days=1)

    groups = {}
    for day in daily:
        parsed = _date(day["date"])
        monday = parsed - timedelta(days=parsed.weekday())
        groups.setdefault(monday, []).append(day)
    weekly = []
    for monday, days in groups.items():
        known = [day for day in days if day["has_interval"]]
        week_start, week_end = max(monday, start), min(monday + timedelta(days=7), end)
        weekly.append({"start": week_start.isoformat(), "end": week_end.isoformat(),
                       "seconds": sum(day["seconds"] for day in known) if known else None,
                       "estimated_seconds": sum(day["estimated_seconds"] for day in known),
                       "estimated_days": sum(bool(day["estimated_count"]) for day in known),
                       "recorded_days": len(known), "known_days": len(known),
                       "elapsed_days": sum(not day["future"] for day in days),
                       "future": all(day["future"] for day in days),
                       "partial": week_start != monday or week_end != monday + timedelta(days=7)
                       or any(not day["has_interval"] or day["unknown_count"] for day in days)})

    summary = _metrics(snapshot.get("summary"), category)
    return {"category": category, "range_start": start.isoformat() if start else None,
            "range_end": end.isoformat() if end else None, "as_of_date": as_of.isoformat() if as_of else None,
            "total_seconds": total, "recorded_days": len(measured),
            "estimated_seconds": sum(day["estimated_seconds"] for day in measured),
            "estimated_count": _count(summary.get("estimated_count")),
            "estimated_days": sum(bool(day["estimated_count"]) for day in measured),
            "average_seconds": total / len(measured) if measured else None,
            "peak_seconds": peak, "peak_dates": peak_dates,
            "peak_estimated": any(day["estimated_seconds"] for day in measured if day["seconds"] == peak),
            "current_streak": current, "longest_streak": longest, "streak_end_date": streak_end,
            "record_count": _count(summary.get("record_count")),
            "unknown_count": _count(summary.get("unknown_count")),
            "excluded_ai_count": _count(summary.get("excluded_ai_count")),
            "has_data": bool(by_date), "has_intervals": bool(measured),
            "elapsed_days": len(elapsed), "unknown_days": len(elapsed) - len(measured),
            "daily": daily, "weekly": weekly}
