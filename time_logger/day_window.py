"""Display-only activity days; stored timestamps and natural-day charts stay intact."""
from datetime import date, datetime, time, timedelta

from .calculator import LOCAL_TZ

DAY_START_HOUR = 7


def recording_date(now=None):
    now = datetime.now(LOCAL_TZ) if now is None else now.astimezone(LOCAL_TZ)
    return (now - timedelta(hours=DAY_START_HOUR)).date()


def day_bounds(day, day_start_hour=0):
    if type(day_start_hour) is not int or not 0 <= day_start_hour <= 23:
        raise ValueError("invalid_day_start_hour")
    parsed = date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError("invalid_timeline_date")
    start = datetime.combine(parsed, time(hour=day_start_hour), LOCAL_TZ)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)
