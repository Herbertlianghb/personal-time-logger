"""Read daily targets from dated Feishu standards; no local target authority."""
from __future__ import annotations

import copy
from datetime import date, datetime
import math

from .base_adapter import _schema_fields
from .calculator import LOCAL_TZ
from .store import digest, now_ms

FIELDS = {"时段": "text", "开始时间": "datetime", "结束时间": "datetime",
          "工作时间标准（h）": "number", "学习时间标准（h）": "number"}


def _day(value):
    if type(value) in (int, float) and math.isfinite(value):
        return datetime.fromtimestamp(value / 1000, LOCAL_TZ).date()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(LOCAL_TZ).date() if parsed.tzinfo else parsed.date()
    return None


def _hours(value):
    if value is None or value == 0:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 24:
        raise ValueError("invalid_daily_target")
    return value


class DailyGoalReader:
    def __init__(self, store, transport, *, settings_url="", clock=now_ms, cache_ttl_ms=60000):
        self.store, self.transport = store, transport
        self.settings_url, self.clock, self.cache_ttl_ms = settings_url, clock, cache_ttl_ms

    def _fetch(self, day, timestamp):
        schema = _schema_fields(self.transport.field_schema())
        if any(schema.get(name, {}).get("type") != kind for name, kind in FIELDS.items()):
            raise ValueError("daily_target_schema_mismatch")
        rows, offset = [], 0
        while True:
            args = ["--limit", "200", "--offset", str(offset)]
            for name in FIELDS:
                args.extend(["--field-id", name])
            page = self.transport._call("+record-list", *args)
            rows.extend(self.transport._records(page))
            if len(rows) > 2000:
                raise ValueError("daily_target_record_bound")
            if not page.get("has_more"):
                break
            following = page.get("next_offset")
            if type(following) is not int or following <= offset:
                raise ValueError("daily_target_pagination_incomplete")
            offset = following
        active = []
        for row in rows:
            fields = row["fields"]
            start, end = _day(fields.get("开始时间")), _day(fields.get("结束时间"))
            if start is not None and end is not None and start <= day <= end:
                active.append(row)
        if len(active) > 1:
            raise ValueError("daily_target_period_overlap")
        fields = active[0]["fields"] if active else {}
        return {"date": day.isoformat(), "source": "feishu", "settings_url": self.settings_url,
                "work_hours": _hours(fields.get("工作时间标准（h）")),
                "study_hours": _hours(fields.get("学习时间标准（h）")),
                "record_id": active[0]["record_id"] if active else None,
                "period_label": fields.get("时段"),
                "valid_from": _day(fields["开始时间"]).isoformat() if active else None,
                "valid_until": _day(fields["结束时间"]).isoformat() if active else None,
                "as_of_ms": timestamp}

    def read(self, day):
        day = date.fromisoformat(day)
        timestamp = self.clock()
        key = "desktop_daily_goals:" + digest(self.transport.table_key)[:24] + ":" + day.isoformat()
        cached = self.store.get_setting(key)
        error, state = None, "fresh"
        if cached and 0 <= timestamp - cached["as_of_ms"] < self.cache_ttl_ms:
            value, state = cached, "cached"
        else:
            try:
                value = self._fetch(day, timestamp)
                self.store.set_setting(key, value)
            except Exception as exc:
                error, state = type(exc).__name__, "stale" if cached else "unavailable"
                value = cached or {"date": day.isoformat(), "source": "feishu", "work_hours": None,
                                   "study_hours": None, "settings_url": self.settings_url, "as_of_ms": None}
        value = copy.deepcopy(value)
        value["cache"] = {"state": state, "refresh_error": error}
        return value
