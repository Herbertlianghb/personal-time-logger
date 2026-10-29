from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from time_logger.base_adapter import ALLOWLIST
from time_logger.calculator import LOCAL_TZ
from time_logger.store import Store, packed
from time_logger.timeline import TimelineReader, _report_ms
from time_logger.formal import prepare_correction, record_fingerprint


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


DAY = "2026-10-25"
LEFT = stamp(DAY + "T00:00:00+08:00")
NOW = stamp("2026-10-27T15:00:00+08:00")
SCHEMA = {"fields": [{"name": name, "type": kind, "id": name} for name, kind in ALLOWLIST.items()]}


def record(rid, start=None, end=None, **fields):
    return {"record_id": rid, "fields": {"时间花销": "虚构边界验收活动", "开始时间": start, "结束时间": end,
                                         "一级分类": ["工作"], "备注": "", **fields}}


class ReadTransport:
    def __init__(self, records):
        self.records = copy.deepcopy(records)
        self.fail = False
        self.calls = 0
        self.bad_pagination = False
        self.page_size = 200

    def field_schema(self):
        if self.fail:
            raise OSError("synthetic_network_failure")
        self.calls += 1
        return copy.deepcopy(SCHEMA)

    def get(self, rid):
        self.calls += 1
        return next((copy.deepcopy(row) for row in self.records if row["record_id"] == rid), None)

    def _call(self, command, *args):
        assert command == "+record-list"
        self.calls += 1
        opts = dict(zip(args[::2], args[1::2]))
        conditions = json.loads(opts["--filter-json"])["conditions"]
        def passes(row):
            for field, op, expr in conditions:
                value = row["fields"].get(field)
                if value is None:
                    return False
                day = expr[len("ExactDate("):-1]
                point = stamp(day + "T00:00:00+08:00")
                if op == "==" and not point <= value < point + 86400000:
                    return False
                if op == "<" and not value < point:
                    return False
                if op == ">" and not value > point:
                    return False
            return True
        selected = [row for row in self.records if passes(row)]
        offset = int(opts["--offset"])
        page = selected[offset:offset + self.page_size]
        names = [field["name"] for field in SCHEMA["fields"]]
        more = offset + len(page) < len(selected)
        return {"fields": names, "record_id_list": [row["record_id"] for row in page],
                "data": [[row["fields"].get(name) for name in names] for row in page],
                "has_more": more, "next_offset": offset if self.bad_pagination else offset + len(page)}


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ptl-timeline-")
        self.store = Store(Path(self.temp.name) / "evidence.sqlite3", "test", ["self"])
        self.clock = NOW

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reader(self, rows):
        self.transport = ReadTransport(rows)
        return TimelineReader(self.store, self.transport, clock=lambda: self.clock)

    def evidence(self, content="虚构验收来源", created=None):
        result = self.store.ingest({"app_scope": "test", "message_id": "synthetic-" + str(self.clock),
                                   "chat_id": "self", "sender_key": "self", "created_at_ms": created or LEFT + 12 * 3600000,
                                   "content": content, "source": "authorized_import"})
        self.clock += 1
        return result["observation_id"]

    def mapped(self, row, *, aid="a1", human=True, revision=1, start=None, end=None, scalar=None, subtasks=None):
        oid = self.evidence()
        start = row["fields"]["开始时间"] if start is None else start
        end = row["fields"]["结束时间"] if end is None else end
        body = {"name": row["fields"]["时间花销"], "actor": "human", "role": "primary", "revision": revision,
                "activity_id": aid, "status": "ended" if end else "active", "category": "工作", "project": None,
                "start": {"kind": "explicit_user" if start else "unknown", "value_ms": start},
                "end": {"kind": "explicit_user" if end else "unknown", "value_ms": end},
                "evidence_refs": [oid], "reported_duration_seconds": scalar, "uncertainty": "",
                "subtasks": subtasks or [], "human_intervals": [{"start_ms": start, "end_ms": end}] if human and start and end else []}
        row["fields"]["备注"] = f"[PTL:{aid}:day_0]\n" + packed(body)
        with self.store.transaction() as db:
            db.execute("INSERT INTO activities VALUES(?,?,?,?,1,?)", (aid, revision, "unit-test", packed(body), NOW))
            db.execute("INSERT INTO base_mapping VALUES(?,?,?,?,?,?)", (aid, "day_0", row["record_id"], revision, packed(row["fields"]), "test"))
        return body

    def test_legacy_windows_are_not_claimed_as_active_human(self):
        reader = self.reader([record("r1", LEFT + 9 * 3600000, LEFT + 10 * 3600000)])
        result = reader.history(DAY)
        self.assertEqual(result["summary"]["recorded_seconds"], 3600)
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertEqual(result["items"][0]["duration_kind"], "recorded_window")

    def test_estimated_legacy_boundaries_do_not_become_exact_bars(self):
        row = record("estimated", LEFT + 21 * 3600000, LEFT + 21 * 3600000 + 18 * 60000,
                     **{"备注": "结束采用报告时间估算；实际切换分钟未知"})
        result = self.reader([row]).history(DAY)
        item = result["items"][0]
        self.assertIsNone(item["start_ms"])
        self.assertIsNone(item["end_ms"])
        self.assertEqual(item["recorded_intervals"], [])
        self.assertEqual(item["reported_duration_seconds"], 1080)
        self.assertTrue(item["estimated"])
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertIsNone(result["summary"]["recorded_seconds"])

    def test_cross_midnight_starting_preceding_day_is_not_lost(self):
        result = self.reader([record("cross", LEFT - 1800000, LEFT + 3600000)]).history(DAY)
        self.assertEqual(result["items"][0]["recorded_intervals"], [[LEFT, LEFT + 3600000]])
        self.assertEqual(result["summary"]["recorded_seconds"], 3600)

    def test_long_cross_day_record_is_found_without_history_scan(self):
        result = self.reader([record("long", LEFT - 8 * 86400000, LEFT + 2 * 86400000)]).history(DAY)
        self.assertEqual(result["summary"]["recorded_seconds"], 86400)

    def test_overlap_union_is_not_row_sum(self):
        result = self.reader([record("r1", LEFT, LEFT + 3600000), record("r2", LEFT + 1800000, LEFT + 5400000)]).history(DAY)
        self.assertEqual(result["summary"]["recorded_seconds"], 5400)

    def test_future_part_is_clipped_to_trusted_server_now(self):
        self.clock = LEFT + 10 * 3600000
        result = self.reader([record("r1", LEFT + 9 * 3600000, LEFT + 11 * 3600000),
                              record("future", LEFT + 11 * 3600000, LEFT + 12 * 3600000)]).history(DAY)
        self.assertEqual(result["summary"]["recorded_seconds"], 3600)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["recorded_intervals"][-1][-1], self.clock)

    def test_open_activity_does_not_accumulate_human_effort(self):
        row = record("open")
        self.mapped(row, start=LEFT + 9 * 3600000)
        reader = self.reader([row])
        first = reader.history(DAY)
        self.clock += 4 * 3600000
        second = reader.history(DAY)
        self.assertIsNone(first["summary"]["human_seconds"])
        self.assertIsNone(second["summary"]["human_seconds"])
        self.assertIsNone(second["items"][0]["end_ms"])

    def test_verified_mapped_intervals_are_confirmed_human(self):
        row = record("known", LEFT + 9 * 3600000, LEFT + 10 * 3600000)
        self.mapped(row)
        result = self.reader([row]).history(DAY)
        self.assertEqual(result["summary"]["human_seconds"], 3600)
        self.assertEqual(result["items"][0]["formal_state"], "verified_ptl")

    def test_unchanged_mapping_does_not_invent_human_intervals(self):
        row = record("known-window", LEFT, LEFT + 3600000)
        self.mapped(row, human=False)
        result = self.reader([row]).history(DAY)
        self.assertEqual(result["summary"]["recorded_seconds"], 3600)
        self.assertIsNone(result["summary"]["human_seconds"])

    def test_multiple_formal_segments_keep_breaks_and_future_clip(self):
        first = record("segment1", LEFT, LEFT + 600000)
        body = self.mapped(first, end=LEFT + 1800000)
        body["human_intervals"] = [{"start_ms": LEFT, "end_ms": LEFT + 600000},
                                    {"start_ms": LEFT + 1200000, "end_ms": LEFT + 1800000}]
        body["reported_duration_seconds"] = 1800
        first["fields"]["备注"] = "[PTL:a1:day_0]\n" + packed(body)
        second = record("segment2", LEFT + 1200000, LEFT + 1800000,
                        **{"备注": "[PTL:a1:day_1]\n" + packed(body)})
        with self.store.transaction() as db:
            db.execute("UPDATE activities SET body=? WHERE activity_id='a1'", (packed(body),))
            db.execute("UPDATE base_mapping SET baseline=? WHERE activity_id='a1'", (packed(first["fields"]),))
            db.execute("INSERT INTO base_mapping VALUES('a1','day_1','segment2',1,?,'test')", (packed(second["fields"]),))
        self.clock = LEFT + 1500000
        result = self.reader([first, second]).history(DAY)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["human_intervals"], [[LEFT, LEFT + 600000], [LEFT + 1200000, LEFT + 1500000]])
        self.assertEqual(result["summary"]["human_seconds"], 900)
        self.assertEqual(result["items"][0]["duration_seconds"], 900)
        self.assertIsNone(result["items"][0]["record_fingerprint"])

    def test_legacy_selection_has_same_canonical_fingerprint_as_prepare(self):
        row = record("recLegacy001", LEFT, LEFT + 60000)
        reader = self.reader([row])
        item = reader.history(DAY)["items"][0]
        self.assertEqual(item["record_id"], row["record_id"])
        prepared = prepare_correction(self.store, {"base_token": "synthetic-base", "time_table_id": "synthetic-table"}, item["record_id"], item["record_fingerprint"], self.transport)
        self.assertEqual(prepared["record_fingerprint"], item["record_fingerprint"])
        self.assertEqual(record_fingerprint(row["record_id"], {**row["fields"], "开始时间": DAY + "T00:00:00+08:00"},
                                            {field["name"]: field for field in SCHEMA["fields"]}), item["record_fingerprint"])

    def test_linking_legacy_estimate_keeps_estimate_and_no_human(self):
        row = record("recLegacy002", LEFT, LEFT + 1080000, **{"备注": "报告时间估算，实际切换分钟未知"})
        reader = self.reader([row])
        item = reader.history(DAY)["items"][0]
        prepared = prepare_correction(self.store, {"base_token": "synthetic-base", "time_table_id": "synthetic-table"}, item["record_id"], item["record_fingerprint"], self.transport)
        linked = reader.history(DAY)["items"][0]
        self.assertEqual(linked["activity_id"], prepared["activity_id"])
        self.assertEqual(linked["formal_state"], "linked_legacy")
        self.assertTrue(linked["estimated"])
        self.assertIsNone(linked["start_ms"])
        self.assertEqual(linked["human_intervals"], [])
        self.assertEqual(linked["reported_duration_seconds"], 1080)

    def test_one_explicit_legacy_correction_keeps_only_that_boundary(self):
        row = record("recLegacy003", LEFT, LEFT + 1080000, **{"备注": "报告时间估算，实际切换分钟未知"})
        reader = self.reader([row])
        item = reader.history(DAY)["items"][0]
        prepared = prepare_correction(self.store, {"base_token": "synthetic-base", "time_table_id": "synthetic-table"}, item["record_id"], item["record_fingerprint"], self.transport)
        aid = prepared["activity_id"]
        with self.store.transaction() as db:
            body = json.loads(db.execute("SELECT body FROM activities WHERE activity_id=?", (aid,)).fetchone()[0])
            body["end"] = {"kind": "explicit_user", "value_ms": LEFT + 1200000}
            body["reported_duration_seconds"] = None
            db.execute("UPDATE activities SET body=? WHERE activity_id=?", (packed(body), aid))
            self.transport.records[0]["fields"]["结束时间"] = LEFT + 1200000
            db.execute("UPDATE base_mapping SET baseline=? WHERE activity_id=?", (packed(self.transport.records[0]["fields"]), aid))
        linked = reader.history(DAY)["items"][0]
        self.assertIsNone(linked["start_ms"])
        self.assertEqual(linked["end_ms"], LEFT + 1200000)
        self.assertEqual(linked["recorded_intervals"], [])
        self.assertEqual(linked["human_intervals"], [])
        self.assertIsNone(linked["reported_duration_seconds"])

    def test_remote_manual_change_is_authoritative_not_old_local(self):
        row = record("changed", LEFT + 9 * 3600000, LEFT + 10 * 3600000)
        self.mapped(row)
        row["fields"]["结束时间"] += 1800000
        row["fields"]["时间花销"] = "用户在正式表修改后的名称"
        result = self.reader([row]).history(DAY)
        self.assertEqual(result["items"][0]["name"], row["fields"]["时间花销"])
        self.assertEqual(result["summary"]["recorded_seconds"], 5400)
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertEqual(result["items"][0]["formal_state"], "remote_modified")

    def test_newer_unwritten_local_revision_cannot_replace_formal_record(self):
        row = record("old-formal", LEFT, LEFT + 3600000)
        body = self.mapped(row)
        body["name"] = "本地尚未归档的新名称"
        body["revision"] = 2
        with self.store.transaction() as db:
            db.execute("UPDATE activities SET current_valid=0 WHERE activity_id='a1'")
            db.execute("INSERT INTO activities VALUES('a1',2,'new',?,1,?)", (packed(body), NOW + 1))
        result = self.reader([row]).history(DAY)
        self.assertEqual(result["items"][0]["name"], row["fields"]["时间花销"])
        self.assertIsNone(result["summary"]["human_seconds"])

    def test_untimed_mapped_fact_uses_report_day_without_made_up_interval(self):
        row = record("fact")
        self.mapped(row, scalar=1200)
        result = self.reader([row]).history(DAY)
        self.assertEqual(len(result["items"]), 1)
        self.assertIsNotNone(result["items"][0]["report_ms"])
        self.assertEqual(result["items"][0]["human_intervals"], [])
        self.assertEqual(result["summary"]["scalar_seconds"], 1200)
        self.assertFalse(result["summary"]["scalar_is_additive"])

    def sleep_fact(self, *, bedtime="睡觉，刚才是在回顾之前玩手机", wake="醒了", start=None, end=None, aid="sleep"):
        start = LEFT + 3752000 if start is None else start
        end = LEFT + 20997000 if end is None else end
        first, last = self.evidence(bedtime, start), self.evidence(wake, end)
        row = record(aid, **{"时间花销": "睡觉休息", "一级分类": ["休息"]})
        body = self.mapped(row, aid=aid, human=False)
        body.update(status="ended", category="休息", evidence_refs=[first, last])
        body["start"] = {"kind": "unknown", "value_ms": None, "source_id": first, "quote": "睡觉"}
        body["end"] = {"kind": "unknown", "value_ms": None, "source_id": last, "quote": wake}
        self.save_fact(row, body)
        return row, body

    def save_fact(self, row, body):
        row["fields"]["备注"] = f"[PTL:{body['activity_id']}:day_0]\n" + packed(body)
        with self.store.transaction() as db:
            db.execute("UPDATE activities SET body=? WHERE activity_id=? AND current_valid=1", (packed(body), body["activity_id"]))
            db.execute("UPDATE base_mapping SET baseline=? WHERE activity_id=?", (packed(row["fields"]), body["activity_id"]))

    def test_sleep_reports_add_only_a_traceable_estimated_overlay(self):
        row, body = self.sleep_fact()
        before = copy.deepcopy(self.store.current_activities())
        result = self.reader([row]).history(DAY)
        item = result["items"][0]
        windows = item.get("estimated_intervals", [])
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual((window["start_ms"], window["end_ms"]), (LEFT + 3752000, LEFT + 20997000))
        self.assertEqual(window["kind"], "sleep_report_window")
        self.assertIn("估计", window["label"])
        self.assertEqual(window["source_refs"], [
            {"type": "evidence", "id": body["evidence_refs"][0], "report_ms": LEFT + 3752000, "role": "sleep_report"},
            {"type": "evidence", "id": body["evidence_refs"][1], "report_ms": LEFT + 20997000, "role": "wake_report"}])
        self.assertIsNone(item["start_ms"])
        self.assertIsNone(item["end_ms"])
        self.assertEqual(item["human_intervals"], [])
        self.assertEqual(item["recorded_intervals"], [])
        self.assertIsNone(item["duration_seconds"])
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertIsNone(result["summary"]["recorded_seconds"])
        self.assertIsNone(result["summary"]["estimated_seconds"])
        self.assertEqual(self.store.current_activities(), before)
        self.assertEqual(self.transport.records, [row])

    def test_sleep_plan_negation_reference_and_historical_cues_are_not_overlays(self):
        cases = [("准备睡觉", "醒了"), ("等会睡觉", "醒了"), ("今晚睡觉", "醒了"),
                 ("没有睡觉", "醒了"), ("睡觉了吗？", "醒了"), ("朋友说睡觉", "醒了"),
                 ("昨天睡觉", "醒了"), ("睡觉", "还没醒了"), ("睡觉", "她说醒了"),
                 ("睡觉", "明天醒了"), ("睡觉，但我没睡", "醒了")]
        for index, (bedtime, wake) in enumerate(cases):
            with self.subTest(bedtime=bedtime, wake=wake):
                row, _ = self.sleep_fact(bedtime=bedtime, wake=wake, aid="negative" + str(index))
                result = self.reader([row]).history(DAY)
                self.assertFalse(any(item.get("estimated_intervals") for item in result["items"]))

    def test_sleep_overlay_requires_current_non_recalled_sources_even_with_cached_history(self):
        row, body = self.sleep_fact()
        reader = self.reader([row])
        self.assertTrue(reader.history(DAY)["items"][0].get("estimated_intervals"))
        source = self.store.evidence([body["evidence_refs"][0]])[0]
        self.store.ingest({"app_scope": "test", "message_id": source["message_id"], "chat_id": "self",
                           "sender_key": "self", "kind": "recall", "created_at_ms": source["created_at_ms"]})
        result = reader.history(DAY)
        self.assertFalse(result["items"][0].get("estimated_intervals"))
        self.assertEqual(result["cache"]["state"], "fresh")

    def test_sleep_recall_removes_overlay_even_when_formal_read_fails(self):
        row, body = self.sleep_fact()
        reader = self.reader([row])
        first = reader.history(DAY)
        source = self.store.evidence([body["evidence_refs"][0]])[0]
        self.store.ingest({"app_scope": "test", "message_id": source["message_id"], "chat_id": "self",
                           "sender_key": "self", "kind": "recall", "created_at_ms": source["created_at_ms"]})
        self.transport.fail = True
        result = reader.history(DAY)
        self.assertEqual(result["cache"]["state"], "stale")
        self.assertFalse(result["items"][0].get("estimated_intervals"))
        self.assertEqual(result["summary"], first["summary"])
        self.assertNotEqual(result["data_revision"], first["data_revision"])

    def test_sleep_recall_invalidates_old_cursor_instead_of_returning_cached_overlay(self):
        row, body = self.sleep_fact()
        reader = self.reader([row, record("second", LEFT + 12 * 3600000, LEFT + 13 * 3600000)])
        first = reader.history(DAY, limit=1)
        source = self.store.evidence([body["evidence_refs"][0]])[0]
        self.store.ingest({"app_scope": "test", "message_id": source["message_id"], "chat_id": "self",
                           "sender_key": "self", "kind": "recall", "created_at_ms": source["created_at_ms"]})
        with self.assertRaisesRegex(ValueError, "timeline_cursor_expired"):
            reader.history(DAY, cursor=first["next_cursor"], limit=1)
        self.assertFalse(any(item.get("estimated_intervals") for item in self.store.get_setting("desktop_timeline_cache:" + DAY)["items"]))

    def test_sleep_recall_during_outage_returns_a_usable_new_cursor(self):
        row, body = self.sleep_fact()
        reader = self.reader([row, record("second", LEFT + 12 * 3600000, LEFT + 13 * 3600000)])
        reader.history(DAY, limit=1)
        source = self.store.evidence([body["evidence_refs"][0]])[0]
        self.store.ingest({"app_scope": "test", "message_id": source["message_id"], "chat_id": "self",
                           "sender_key": "self", "kind": "recall", "created_at_ms": source["created_at_ms"]})
        self.transport.fail = True
        first = reader.history(DAY, limit=1)
        second = reader.history(DAY, cursor=first["next_cursor"], limit=1)
        self.assertEqual(second["data_revision"], first["data_revision"])
        self.assertEqual(second["items"][0]["record_ids"], ["second"])
        self.assertIsNone(second["next_cursor"])

    def test_sleep_overlay_rejects_untrusted_desktop_missing_future_and_reversed_report_times(self):
        for case in ("desktop", "missing", "future", "reversed", "wrong_old_association"):
            with self.subTest(case=case):
                row, body = self.sleep_fact(aid="badtime" + case)
                oid = body["evidence_refs"][0 if case in {"desktop", "missing"} else 1]
                with self.store.transaction() as db:
                    if case == "desktop":
                        db.execute("UPDATE observations SET source='desktop' WHERE observation_id=?", (oid,))
                    else:
                        value = (None if case == "missing" else NOW + 60000 if case == "future"
                                 else LEFT + 51 * 3600000 if case == "wrong_old_association" else LEFT)
                        db.execute("UPDATE messages SET message_created_at_ms=? WHERE message_key=(SELECT message_key FROM observations WHERE observation_id=?)", (value, oid))
                result = self.reader([row]).history(DAY)
                self.assertFalse(any(item.get("estimated_intervals") for item in result["items"]))

    def test_sleep_overlay_does_not_swallow_a_known_awake_interval(self):
        row, _ = self.sleep_fact()
        awake = record("awake", LEFT + 3 * 3600000, LEFT + 4 * 3600000)
        result = self.reader([row, awake]).history(DAY)
        sleep = next(item for item in result["items"] if item["activity_id"] == "sleep")
        self.assertFalse(sleep.get("estimated_intervals"))
        self.assertEqual(result["summary"]["recorded_seconds"], 3600)

    def test_sleep_overlay_rejects_an_unarchived_awake_window_surrounding_it(self):
        row, _ = self.sleep_fact()
        awake = {"activity_id": "unarchived-awake", "revision": 1, "actor": "human", "role": "primary",
                 "status": "ended", "name": "通宵阅读", "start": {"kind": "explicit_user", "value_ms": LEFT},
                 "end": {"kind": "explicit_user", "value_ms": LEFT + 8 * 3600000}, "evidence_refs": []}
        with self.store.transaction() as db:
            db.execute("INSERT INTO activities VALUES(?,?,?,?,1,?)", (awake["activity_id"], 1, "unit-test", packed(awake), NOW))
        result = self.reader([row]).history(DAY)
        self.assertFalse(result["items"][0].get("estimated_intervals"))

    def test_sleep_overlay_rejects_an_interior_new_awake_report_but_not_a_clarification(self):
        for case in ("clarification", "new_activity"):
            with self.subTest(case=case):
                row, sleep = self.sleep_fact(aid="sleep" + case)
                oid = self.evidence("这两项合在一起算" if case == "clarification" else "现在开始看书", LEFT + 4200000)
                awake = record("phone" + case)
                body = self.mapped(awake, aid="phone" + case, human=False)
                body["evidence_refs"] = [sleep["evidence_refs"][0], oid]
                body["start"] = {"kind": "unknown", "value_ms": None, "source_id": sleep["evidence_refs"][0], "quote": "睡觉"}
                if case == "new_activity":
                    body["start"] = {"kind": "report_anchor", "value_ms": LEFT + 4200000, "source_id": oid, "quote": "现在开始看书"}
                self.save_fact(awake, body)
                result = self.reader([row, awake]).history(DAY)
                item = next(item for item in result["items"] if item["activity_id"] == sleep["activity_id"])
                self.assertEqual(bool(item.get("estimated_intervals")), case == "clarification")

    def test_sleep_overlay_is_clipped_across_midnight_without_replacing_source_times(self):
        row, _ = self.sleep_fact(start=LEFT - 3600000, end=LEFT + 6 * 3600000)
        reader = self.reader([row])
        previous = reader.history("2026-10-24")["items"][0]["estimated_intervals"][0]
        current = reader.history(DAY)["items"][0]["estimated_intervals"][0]
        self.assertEqual((previous["start_ms"], previous["end_ms"]), (LEFT - 3600000, LEFT))
        self.assertEqual((current["start_ms"], current["end_ms"]), (LEFT, LEFT + 6 * 3600000))
        self.assertEqual(current["source_refs"], previous["source_refs"])

    def test_sleep_overlay_requires_verified_sleep_identity_and_ended_status(self):
        for case in ("renamed", "remote_changed", "cancelled", "active", "exact"):
            with self.subTest(case=case):
                row, body = self.sleep_fact(aid="identity" + case)
                if case == "renamed":
                    body["name"] = row["fields"]["时间花销"] = "普通休息"
                elif case in {"cancelled", "active"}:
                    body["status"] = case
                elif case == "exact":
                    body["start"] = {"kind": "explicit_user", "value_ms": LEFT + 3600000}
                    body["end"] = {"kind": "explicit_user", "value_ms": LEFT + 6 * 3600000}
                self.save_fact(row, body)
                if case == "remote_changed":
                    row["fields"]["时间花销"] = "正式表人工修改后的活动"
                result = self.reader([row]).history(DAY)
                self.assertFalse(any(item.get("estimated_intervals") for item in result["items"]))

    def test_ai_is_separate_and_never_added_to_human(self):
        body = {"activity_id": "ai1", "revision": 1, "name": "虚构AI后台", "actor": "ai", "role": "primary", "status": "ended",
                "start": {"kind": "explicit_user", "value_ms": LEFT}, "end": {"kind": "explicit_user", "value_ms": LEFT + 3600000},
                "evidence_refs": [], "category": "工作", "project": None}
        with self.store.transaction() as db:
            db.execute("INSERT INTO activities VALUES('ai1',1,'test',?,1,?)", (packed(body), NOW))
        result = self.reader([]).history(DAY)
        self.assertEqual(result["summary"]["ai_seconds"], 3600)
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertEqual(result["items"][0]["source"], "backend_accepted_ai")

    def test_unverified_desktop_clock_is_not_a_precise_report_anchor(self):
        source = {"source": "desktop", "created_at_ms": LEFT, "observed_at_ms": NOW,
                  "desktop_context": {"report_time_trust": "device_clock_unverified", "server_received_at_ms": NOW}}
        self.assertIsNone(_report_ms([source]))
        source["desktop_context"]["report_time_trust"] = "near_server_clock"
        self.assertEqual(_report_ms([source]), LEFT)

    def test_repeated_record_changes_during_date_reads_are_rejected(self):
        reader = self.reader([record("r1", LEFT, LEFT + 60000)])
        original = self.transport._call
        def changing(*args):
            result = original(*args)
            if self.transport.calls == 2:
                self.transport.records[0]["fields"]["结束时间"] += 60000
            return result
        self.transport._call = changing
        with self.assertRaisesRegex(ValueError, "remote_changed_during_read"):
            reader.history(DAY)
        self.assertIsNone(self.store.get_setting("desktop_timeline_cache:" + DAY))

    def test_parent_subtasks_are_not_added_to_union(self):
        row = record("parent", LEFT, LEFT + 3600000)
        self.mapped(row, subtasks=[{"name": "虚构子任务", "actor": "human", "status": "ended", "evidence_refs": []}])
        result = self.reader([row]).history(DAY)
        self.assertEqual(result["summary"]["human_seconds"], 3600)
        self.assertEqual(len(result["items"][0]["subtasks"]), 1)

    def test_empty_day_is_no_available_records_not_zero_work(self):
        result = self.reader([]).history(DAY)
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["summary"]["human_seconds"])
        self.assertIsNone(result["summary"]["recorded_seconds"])
        self.assertEqual(result["completeness"], "partial")

    def test_cache_does_not_call_feishu_per_refresh(self):
        reader = self.reader([record("r", LEFT, LEFT + 60000)])
        first = reader.history(DAY)
        calls = self.transport.calls
        second = reader.history(DAY, refresh=True)
        self.assertEqual(self.transport.calls, calls)
        self.assertEqual(first["data_revision"], second["data_revision"])
        self.assertEqual(second["cache"]["state"], "cached")

    def test_failure_keeps_previous_snapshot_and_totals(self):
        reader = self.reader([record("r", LEFT, LEFT + 60000)])
        first = reader.history(DAY)
        self.transport.fail = True
        self.clock += 60001
        second = reader.history(DAY)
        self.assertEqual(second["cache"]["state"], "stale")
        self.assertEqual(first["data_revision"], second["data_revision"])
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["as_of_ms"], second["as_of_ms"])

    def test_first_read_failure_does_not_claim_empty_success(self):
        reader = self.reader([])
        self.transport.fail = True
        with self.assertRaises(OSError):
            reader.history(DAY)
        self.assertIsNone(self.store.get_setting("desktop_timeline_cache:" + DAY))

    def test_page_and_summary_use_same_snapshot_revision(self):
        reader = self.reader([record("a", LEFT, LEFT + 60000), record("b", LEFT + 120000, LEFT + 180000)])
        first = reader.history(DAY, limit=1)
        self.transport.records = []
        self.clock += 60001
        second = reader.history(DAY, cursor=first["next_cursor"], limit=1)
        self.assertEqual(second["data_revision"], first["data_revision"])
        self.assertEqual(second["summary"], first["summary"])
        self.assertEqual(second["items"][0]["record_ids"], ["b"])
        self.assertIsNone(second["next_cursor"])

    def test_incomplete_upstream_pagination_is_not_cached(self):
        reader = self.reader([record("a", LEFT, LEFT + 60000), record("b", LEFT + 120000, LEFT + 180000)])
        self.transport.page_size = 1
        self.transport.bad_pagination = True
        with self.assertRaisesRegex(ValueError, "incomplete_timeline_pagination"):
            reader.history(DAY)
        self.assertIsNone(self.store.get_setting("desktop_timeline_cache:" + DAY))

    def test_upstream_pages_complete_before_any_totals_are_returned(self):
        reader = self.reader([record("a", LEFT, LEFT + 60000), record("b", LEFT + 120000, LEFT + 180000)])
        self.transport.page_size = 1
        result = reader.history(DAY)
        self.assertEqual(result["summary"]["recorded_seconds"], 120)
        self.assertEqual(result["total_items"], 2)


if __name__ == "__main__":
    unittest.main()
