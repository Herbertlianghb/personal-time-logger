from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Taipei")


def interval_union(intervals):
    ordered = sorted(intervals)
    result = []
    for start, end in ordered:
        if type(start) is not int or type(end) is not int or end < start:
            raise ValueError("invalid_interval")
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def seconds(intervals):
    return sum(end - start for start, end in interval_union(intervals)) / 1000


def split_days(intervals):
    result = {}
    for start, end in interval_union(intervals):
        cursor = start
        while cursor < end:
            local = datetime.fromtimestamp(cursor / 1000, LOCAL_TZ)
            boundary = int((local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp() * 1000)
            stop = min(end, boundary)
            result.setdefault(local.date().isoformat(), []).append([cursor, stop])
            cursor = stop
    return {day: seconds(items) for day, items in result.items()}


def summarize(blocks):
    human, ai = [], []
    unknown = []
    windows = []
    for block in blocks:
        if block.get("_acceptance_test"):
            continue
        if block.get("role") == "detail" or block.get("status") in {"recalled", "cancelled"}:
            continue
        intervals = [[i["start_ms"], i["end_ms"]] for i in block.get("human_intervals", [])]
        if block.get("actor") == "human":
            human.extend(intervals)
            if not intervals:
                unknown.append(block.get("activity_id"))
        else:
            start, end = block["start"]["value_ms"], block["end"]["value_ms"]
            if start is not None and end is not None:
                ai.append([start, end])
        start, end = block["start"]["value_ms"], block["end"]["value_ms"]
        if start is not None and end is not None:
            windows.append({"activity_id": block.get("activity_id"), "reported_window_seconds": (end - start) / 1000})
    return {"human_effort_seconds": seconds(human), "human_by_day": split_days(human),
            "ai_background_union_seconds": seconds(ai), "unknown_activities": unknown,
            "reported_windows": windows, "completeness": "partial", "timezone": "Asia/Taipei"}
