from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from time_logger.base_adapter import BaseAdapter, BaseSyncError, HttpBaseTransport, LarkCliTransport, TransportError
from time_logger.store import Store, packed


WORK = Path(__file__).resolve().parents[3] / "work"
SCHEMA = {"fields": [
    {"name": "时间花销", "type": "text", "id": "title"},
    {"name": "备注", "type": "text", "id": "note"},
    {"name": "结果记录", "type": "text", "id": "result"},
    {"name": "开始时间", "type": "datetime", "id": "start"},
    {"name": "结束时间", "type": "datetime", "id": "end"},
    {"name": "一级分类", "type": "select", "id": "category", "options": [{"name": "TEST_ONLY"}]},
    {"name": "二级项目", "type": "select", "id": "project", "options": [{"name": "TEST_ONLY"}]},
    {"name": "持续时长（min）", "type": "formula", "id": "formula"},
]}


def timestamp(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def block(intervals=(), **changes):
    start = min((x[0] for x in intervals), default=None)
    end = max((x[1] for x in intervals), default=None)
    result = {"name": "PERSONAL_TIME_LOGGER_TEST 虚构活动", "actor": "human", "role": "primary",
              "status": "ended", "category": "TEST_ONLY", "project": None,
              "start": {"value_ms": start, "kind": "explicit_user" if intervals else "unknown"},
              "end": {"value_ms": end, "kind": "explicit_user" if intervals else "unknown"},
              "human_intervals": [{"start_ms": a, "end_ms": b} for a, b in intervals],
              "evidence_refs": ["synthetic-test-evidence"], "subtasks": [],
              "reported_duration_seconds": None, "uncertainty": "虚构测试，不是实际时间记录。", "result": ""}
    result.update(changes)
    return result


def seed(store, aid, body, revision=1):
    with store.transaction() as db:
        db.execute("UPDATE activities SET current_valid=0 WHERE activity_id=?", (aid,))
        db.execute("INSERT INTO activities VALUES(?,?,?,?,1,0)", (aid, revision, "synthetic-test", packed(body)))


class FakeTransport:
    client_token_enforced = False
    read_only = False

    def __init__(self):
        self.schema = copy.deepcopy(SCHEMA)
        self.table_key = str(uuid.uuid4())
        self.records, self.tokens = {}, []
        self.writes = 0
        self.lose_create = self.lose_update = self.fail_create_before = self.bad_readback = False

    def field_schema(self):
        return self.schema

    def find_marker(self, marker):
        return [copy.deepcopy(r) for r in self.records.values() if marker in (r["fields"].get("备注") or "")]

    def get(self, record_id):
        return copy.deepcopy(self.records.get(record_id))

    def create(self, fields, client_token):
        self.tokens.append(client_token)
        self.writes += 1
        if self.fail_create_before:
            raise TransportError("network_unknown", outcome_unknown=True)
        rid = "rec_" + str(uuid.uuid4())
        self.records[rid] = {"record_id": rid, "fields": copy.deepcopy(fields)}
        if self.lose_create:
            self.lose_create = False
            raise TransportError("response_lost", outcome_unknown=True)
        if self.bad_readback:
            self.records[rid]["fields"]["时间花销"] = "unexpected remote value"
        return rid

    def update(self, record_id, fields):
        self.writes += 1
        self.records[record_id]["fields"].update(copy.deepcopy(fields))
        if self.lose_update:
            self.lose_update = False
            raise TransportError("response_lost", outcome_unknown=True)


class BaseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="base-unit-")
        self.store = Store(Path(self.tmp.name) / "db.sqlite3", "test", ["self"])
        self.transport = FakeTransport()
        self.adapter = BaseAdapter(self.store, self.transport, SCHEMA)
        self.aid = "test_activity"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_report_window_preserves_activity_dates_without_creating_effort_or_writes(self):
        body = block(start={"value_ms": 1000, "kind": "report_anchor"},
                     end={"value_ms": 9000, "kind": "report_anchor"})
        seed(self.store, self.aid, body)
        result = self.adapter.sync(self.aid, 1)
        after = result["segments"][0]["after"]
        self.assertEqual(after["开始时间"], 1000)
        self.assertEqual(after["结束时间"], 9000)
        self.assertIn('"value_ms":1000', after["备注"])
        claims = json.loads(after["备注"].split("\n", 1)[1])
        self.assertEqual(claims["counting"], "activity_window")
        self.assertIn("不证明连续人工投入", claims["display_note"])
        self.assertEqual(self.store.current_activities()[0]["human_intervals"], [])
        self.assertEqual(self.transport.writes, 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sync_operations").fetchone()[0], 0)

    def test_single_known_boundary_is_preserved_and_estimates_stay_unknown(self):
        for key in ("start", "end"):
            for kind in ("explicit_user", "report_anchor", "estimate", "unknown"):
                with self.subTest(key=key, kind=kind):
                    body = block(**{key: {"value_ms": 1000, "kind": kind}})
                    aid = key + "_" + kind
                    seed(self.store, aid, body)
                    after = self.adapter.sync(aid, 1)["segments"][0]["after"]
                    remote = "开始时间" if key == "start" else "结束时间"
                    other = "结束时间" if key == "start" else "开始时间"
                    self.assertEqual(after[remote], 1000 if kind in {"explicit_user", "report_anchor"} else None)
                    self.assertIsNone(after[other])
                    claims = json.loads(after["备注"].split("\n", 1)[1])
                    self.assertEqual(claims["counting"], "partial_activity_window" if kind in {"explicit_user", "report_anchor"} else "unknown_effort")

    def test_single_day_fact_becomes_window_in_place(self):
        seed(self.store, self.aid, block())
        first = self.adapter.sync(self.aid, 1, False)
        rid = first["segments"][0]["record_id"]
        seed(self.store, self.aid, block(
            start={"value_ms": 1000, "kind": "explicit_user"},
            end={"value_ms": 9000, "kind": "report_anchor"}), 2)
        second = self.adapter.sync(self.aid, 2, False)
        self.assertEqual(second["status"], "synced")
        self.assertEqual(second["segments"][0]["record_id"], rid)
        self.assertEqual(len(self.transport.records), 1)
        self.assertEqual(self.transport.get(rid)["fields"]["结束时间"], 9000)

    def test_report_window_cross_midnight_keeps_original_claims_and_no_effort(self):
        a, b = timestamp("2026-10-27T23:30:00+08:00"), timestamp("2026-10-28T00:30:00+08:00")
        seed(self.store, self.aid, block(
            start={"value_ms": a, "kind": "report_anchor"},
            end={"value_ms": b, "kind": "report_anchor"}))
        segments = self.adapter.sync(self.aid, 1)["segments"]
        self.assertEqual(len(segments), 2)
        self.assertEqual([s["after"]["结束时间"] - s["after"]["开始时间"] for s in segments], [1800000, 1800000])
        for segment in segments:
            fields = segment["after"]
            claims = json.loads(fields["备注"].split("\n", 1)[1])
            self.assertEqual(claims["start"]["value_ms"], a)
            self.assertEqual(claims["end"]["value_ms"], b)
            self.assertEqual(claims["segment_start_ms"], fields["开始时间"])
            self.assertEqual(claims["segment_end_ms"], fields["结束时间"])
            self.assertEqual(claims["counting"], "activity_window")
        self.assertEqual(self.store.current_activities()[0]["human_intervals"], [])

    def test_human_segments_keep_pause_gaps_instead_of_filling_activity_window(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1000), (a + 2000, a + 3000)],
            start={"value_ms": a - 1000, "kind": "explicit_user"},
            end={"value_ms": a + 4000, "kind": "explicit_user"}))
        segments = self.adapter.sync(self.aid, 1)["segments"]
        self.assertEqual([(s["after"]["开始时间"], s["after"]["结束时间"]) for s in segments],
                         [(a, a + 1000), (a + 2000, a + 3000)])
        self.assertTrue(all(json.loads(s["after"]["备注"].split("\n", 1)[1])["counting"] == "explicit_human_interval" for s in segments))

    def test_activity_window_does_not_invoke_human_overlap_guard(self):
        body = block(start={"value_ms": 1000, "kind": "report_anchor"},
                     end={"value_ms": 9000, "kind": "report_anchor"})
        seed(self.store, self.aid, body)
        self.transport.find_overlaps = lambda *args: self.fail("activity window queried as human effort")
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "synced")

    def test_zero_length_human_interval_does_not_promote_full_window_to_effort(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a)],
            start={"value_ms": a, "kind": "explicit_user"},
            end={"value_ms": a + 7200000, "kind": "explicit_user"}))
        self.transport.find_overlaps = lambda *args: self.fail("zero human interval queried as continuous effort")
        segments = self.adapter.sync(self.aid, 1)["segments"]
        self.assertEqual(len(segments), 1)
        after = segments[0]["after"]
        self.assertEqual((after["开始时间"], after["结束时间"]), (a, a + 7200000))
        claims = json.loads(after["备注"].split("\n", 1)[1])
        self.assertEqual(claims["counting"], "activity_window")
        self.assertIn("不证明连续人工投入", claims["display_note"])
        self.assertEqual(self.store.current_activities()[0]["human_intervals"], [{"start_ms": a, "end_ms": a}])

    def test_explicit_human_guard_ignores_only_marked_non_effort_windows(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1000)]))
        self.transport.find_overlaps = lambda start, end: [{"record_id": "other_window", "fields": {
            "备注": "[PTL:other:fact]\n" + packed({"counting": "activity_window", "status": "ended"})}}]
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "synced")

    def test_recalled_activity_never_reprojects_stale_known_boundaries(self):
        seed(self.store, self.aid, block(status="recalled",
            start={"value_ms": 1000, "kind": "explicit_user"},
            end={"value_ms": 9000, "kind": "report_anchor"}))
        after = self.adapter.sync(self.aid, 1)["segments"][0]["after"]
        self.assertIsNone(after["开始时间"])
        self.assertIsNone(after["结束时间"])
        claims = json.loads(after["备注"].split("\n", 1)[1])
        self.assertEqual(claims["status"], "recalled")
        self.assertIn("审计", claims["display_note"])

    def test_cross_midnight_split_preserves_one_activity(self):
        a, b = timestamp("2026-10-27T23:30:00+08:00"), timestamp("2026-10-28T00:30:00+08:00")
        seed(self.store, self.aid, block([(a, b)]))
        segments = self.adapter.sync(self.aid, 1)["segments"]
        self.assertEqual(len(segments), 2)
        self.assertEqual([s["after"]["结束时间"] - s["after"]["开始时间"] for s in segments], [1800000, 1800000])

    def test_overlapping_subintervals_use_union(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1800000), (a + 900000, a + 2700000)]))
        segments = self.adapter.sync(self.aid, 1)["segments"]
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["after"]["结束时间"] - segments[0]["after"]["开始时间"], 2700000)

    def test_ai_and_detail_do_not_generate_additive_rows(self):
        for changes in ({"actor": "ai"}, {"role": "detail"}):
            with self.subTest(changes=changes):
                seed(self.store, str(uuid.uuid4()), block(**changes))
                aid = self.store.current_activities()[-1]["activity_id"]
                self.assertEqual(self.adapter.sync(aid, 1, False)["segments"], [])
        self.assertEqual(self.transport.writes, 0)

    def test_unknown_classification_not_created(self):
        seed(self.store, self.aid, block(category="不存在的分类", project="不应创建的项目"))
        after = self.adapter.sync(self.aid, 1)["segments"][0]["after"]
        self.assertIsNone(after["一级分类"])
        self.assertIsNone(after["二级项目"])
        self.assertNotIn("持续时长（min）", after)

    def test_success_readback_and_duplicate_are_idempotent(self):
        seed(self.store, self.aid, block())
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "synced")
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["segments"][0]["status"], "already_synced")
        self.assertEqual(len(self.transport.records), 1)
        self.assertEqual(self.transport.writes, 1)
        self.assertEqual(uuid.UUID(self.transport.tokens[0]).version, 4)

    def test_remote_manual_change_stops_even_same_revision(self):
        seed(self.store, self.aid, block())
        self.adapter.sync(self.aid, 1, False)
        next(iter(self.transport.records.values()))["fields"]["时间花销"] = "人工改动"
        result = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(result["reason"], "remote_manual_change")
        self.assertEqual(self.transport.writes, 1)

    def test_old_revision_never_writes(self):
        seed(self.store, self.aid, block(), 1)
        seed(self.store, self.aid, block(result="新结果"), 2)
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "superseded")
        self.assertEqual(self.transport.writes, 0)

    def test_schema_change_stops(self):
        seed(self.store, self.aid, block())
        self.transport.schema["fields"][0]["type"] = "formula"
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "schema_conflict")
        self.assertEqual(self.transport.writes, 0)

    def test_live_option_addition_refreshes_long_lived_adapter_and_replays_once(self):
        new_project = "TEST_NEW_PROJECT"
        seed(self.store, self.aid, block(project=new_project))
        self.transport.schema["fields"][6]["options"].append({"name": new_project})
        first = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(first["status"], "synced")
        record = next(iter(self.transport.records.values()))
        self.assertEqual(record["fields"]["二级项目"], [new_project])
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["segments"][0]["status"], "already_synced")
        self.assertEqual(self.transport.writes, 1)

    def test_live_option_replacement_uses_current_options(self):
        new_category = "TEST_RENAMED_CATEGORY"
        seed(self.store, self.aid, block(category=new_category))
        self.transport.schema["fields"][5]["options"] = [{"name": new_category}]
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "synced")
        self.assertEqual(next(iter(self.transport.records.values()))["fields"]["一级分类"], [new_category])

    def test_option_refresh_cannot_hide_field_identity_or_multiplicity_change(self):
        seed(self.store, self.aid, block())
        for change in ({"id": "replacement-field"}, {"multiple": True}):
            with self.subTest(change=change):
                self.transport.schema = copy.deepcopy(SCHEMA)
                self.transport.schema["fields"][6].update(change)
                self.transport.schema["fields"][6]["options"].append({"name": "TEST_NEW_PROJECT"})
                self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "schema_conflict")
                self.assertEqual(self.transport.writes, 0)

    def test_lost_create_response_recovers_marker_without_duplicate(self):
        seed(self.store, self.aid, block())
        self.transport.lose_create = True
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "outcome_unknown")
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["segments"][0]["status"], "recovered")
        self.assertEqual(len(self.transport.records), 1)
        self.assertEqual(self.transport.writes, 1)

    def test_unknown_create_not_found_is_not_retried(self):
        seed(self.store, self.aid, block())
        self.transport.fail_create_before = True
        self.adapter.sync(self.aid, 1, False)
        result = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(self.transport.writes, 1)

    def test_lost_update_response_recovers_despite_old_baseline(self):
        seed(self.store, self.aid, block())
        self.adapter.sync(self.aid, 1, False)
        seed(self.store, self.aid, block(result="修订结果"), 2)
        self.transport.lose_update = True
        self.assertEqual(self.adapter.sync(self.aid, 2, False)["status"], "outcome_unknown")
        self.assertEqual(self.adapter.sync(self.aid, 2, False)["segments"][0]["status"], "recovered")
        self.assertEqual(self.transport.writes, 2)

    def test_revision_retires_old_timed_segment_without_deletion(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1800000)]))
        self.adapter.sync(self.aid, 1, False)
        seed(self.store, self.aid, block(status="recalled"), 2)
        result = self.adapter.sync(self.aid, 2, False)
        self.assertEqual(result["status"], "synced")
        self.assertTrue(all(r["fields"]["开始时间"] is None for r in self.transport.records.values()))
        self.assertTrue(all(r["fields"]["结束时间"] is None for r in self.transport.records.values()))
        retired = [r for r in self.transport.records.values() if "已被修订替代" in r["fields"]["时间花销"]]
        self.assertEqual(len(retired), 1)
        claims = json.loads(retired[0]["fields"]["备注"].split("\n", 1)[1])
        self.assertEqual(claims["status"], "superseded")
        self.assertIn("不计有效活动时长", claims["display_note"])

    def test_overlapping_primary_activities_require_reconciliation(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1800000)]))
        seed(self.store, "other", block([(a + 1, a + 1000)]))
        with self.assertRaisesRegex(BaseSyncError, "overlapping_primary"):
            self.adapter.sync(self.aid, 1, False)
        self.assertEqual(self.transport.writes, 0)

    def test_readback_mismatch_does_not_confirm_success(self):
        seed(self.store, self.aid, block())
        self.transport.bad_readback = True
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["reason"], "readback_mismatch")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM base_mapping").fetchone()[0], 0)

    def test_duplicate_marker_is_conflict(self):
        seed(self.store, self.aid, block())
        for rid in ("a", "b"):
            self.transport.records[rid] = {"record_id": rid, "fields": {"备注": "[PTL:test_activity:fact]"}}
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["reason"], "duplicate_business_marker")

    def test_read_only_transport_never_writes(self):
        seed(self.store, self.aid, block())
        self.transport.read_only = True
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "read_only")
        self.assertEqual(self.transport.writes, 0)

    def test_existing_remote_overlap_prevents_duplicate_time(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1000)]))
        self.transport.find_overlaps = lambda start, end: [{"record_id": "existing_personal_row", "fields": {"备注": "人工已有记录"}}]
        result = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(result["reason"], "remote_time_overlap")
        self.assertEqual(self.transport.writes, 0)

    def test_native_http_transport_blocks_direct_formal_mutation(self):
        class NoRequests:
            def request(self, *args, **kwargs):
                raise AssertionError("must not contact service")
        transport = HttpBaseTransport(NoRequests(), "example-base", "example-time-table")
        with self.assertRaisesRegex(BaseSyncError, "formal_base_read_only"):
            transport.update("rec-test", {"备注": "forbidden"})

    def test_existing_time_management_dimension_is_used(self):
        self.transport.schema["fields"].append({"id": "dimension", "name": "维度", "type": "select",
                                                "options": [{"name": "时间管理"}]})
        adapter = BaseAdapter(self.store, self.transport, self.transport.schema)
        seed(self.store, self.aid, block())
        self.assertEqual(adapter.sync(self.aid, 1)["segments"][0]["after"]["维度"], ["时间管理"])

    def test_missing_revision_classification_preserves_confirmed_fields_and_baseline(self):
        seed(self.store, self.aid, block(category="TEST_ONLY", project="TEST_ONLY"))
        created = self.adapter.sync(self.aid, 1, False)
        rid = created["segments"][0]["record_id"]
        seed(self.store, self.aid, block(category=None, project=None, result="新结果"), 2)
        self.assertEqual(self.adapter.sync(self.aid, 2, False)["status"], "synced")
        remote = self.transport.get(rid)["fields"]
        self.assertEqual(remote["一级分类"], ["TEST_ONLY"])
        self.assertEqual(remote["二级项目"], ["TEST_ONLY"])
        operation = json.loads(self.store.db.execute(
            "SELECT body FROM sync_operations WHERE activity_id=? AND revision=2", (self.aid,)).fetchone()[0])
        self.assertNotIn("一级分类", operation)
        self.assertNotIn("二级项目", operation)
        self.transport.records[rid]["fields"]["一级分类"] = ["人工修正"]
        self.assertEqual(self.adapter.sync(self.aid, 2, False)["reason"], "remote_manual_change")

    def test_trusted_test_activity_is_excluded_before_any_remote_request(self):
        a = timestamp("2026-10-27T16:00:00+08:00")
        seed(self.store, self.aid, block([(a, a + 1800000)], _acceptance_test=True))
        for method in ("field_schema", "find_marker", "find_overlaps", "get", "create", "update"):
            setattr(self.transport, method, lambda *args: self.fail("acceptance test contacted remote Base"))
        result = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(result["status"], "test_excluded")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sync_operations").fetchone()[0], 0)

    def test_registered_test_source_is_excluded_even_without_activity_flag(self):
        message = {"app_scope": "test", "message_id": "om_trusted_test_source", "chat_id": "self-chat",
                   "sender_key": "self", "sender_type": "user", "message_type": "text",
                   "created_at_ms": timestamp("2026-10-27T16:00:00+08:00"),
                   "content": "今天16:00至16:30学习。", "kind": "receive"}
        oid = self.store.ingest(message)["observation_id"]
        self.store.tag_test_messages([message["message_id"]])
        a = message["created_at_ms"]
        seed(self.store, self.aid, block([(a, a + 1800000)], evidence_refs=[oid]))
        for method in ("field_schema", "find_marker", "find_overlaps", "get", "create", "update"):
            setattr(self.transport, method, lambda *args: self.fail("registered test source contacted remote Base"))
        self.assertTrue(self.store.evidence([oid])[0]["is_acceptance_test"])
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "test_excluded")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sync_operations").fetchone()[0], 0)


@unittest.skipUnless(os.environ.get("PTL_RUN_LIVE_BASE_TESTS") == "1", "explicit live-test switch required")
class LiveBaseAdapterTests(unittest.TestCase):
    """Real writes are confined to the explicitly created disposable test Base."""
    evidence = []
    evidence_file = "base-live-test-evidence.json"

    def setUp(self):
        resource_file = Path(os.environ["PTL_LIVE_BASE_RESOURCE"])
        specification = json.loads(resource_file.read_text(encoding="utf-8-sig"))
        resource = specification["data"]
        if specification.get("allow_live_test_writes") is not True or resource["base"]["name"] != "PERSONAL_TIME_LOGGER_TEST_ONLY":
            raise RuntimeError("unexpected live-test Base")
        self.transport = LarkCliTransport(os.environ["PTL_LIVE_LARK_PROFILE"], resource["base"]["base_token"],
                                          resource["table"]["id"], WORK.parent, allow_formal_write=True)
        self.schema = self.transport.field_schema()
        self.aid = "live_" + uuid.uuid4().hex
        self.store = Store(WORK / (self.aid + ".sqlite3"), "test", ["self"])
        self.adapter = BaseAdapter(self.store, self.transport, self.schema)

    def tearDown(self):
        self.store.close()

    @classmethod
    def tearDownClass(cls):
        (WORK / cls.evidence_file).write_text(packed({
            "type": "real_official_cli_test_Base_only", "cases": cls.evidence,
            "formal_Base_writes": 0}), encoding="utf-8")

    def test_live_create_update_repeat_and_manual_conflict(self):
        seed(self.store, self.aid, block())
        created = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(created["status"], "synced")
        rid = created["segments"][0]["record_id"]
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["segments"][0]["status"], "already_synced")
        seed(self.store, self.aid, block(result="虚构验收修订"), 2)
        updated = self.adapter.sync(self.aid, 2, False)
        self.assertEqual(updated["status"], "synced")
        self.assertEqual(self.transport.get(rid)["fields"]["结果记录"], "虚构验收修订")
        self.assertEqual(self.adapter.sync(self.aid, 1, False)["status"], "superseded")
        self.transport.update(rid, {"时间花销": "PERSONAL_TIME_LOGGER_TEST 模拟人工修正"})
        seed(self.store, self.aid, block(result="不得覆盖人工修正"), 3)
        conflict = self.adapter.sync(self.aid, 3, False)
        self.assertEqual(conflict["reason"], "remote_manual_change")
        self.evidence.append({"case": "create_update_repeat_stale_manual_conflict", "activity_id": self.aid,
                              "record_id": rid, "created": created, "updated": updated, "conflict": conflict})

    def test_live_unknown_create_outcome_recovered_by_marker(self):
        original = self.transport.create

        def create_with_lost_response(fields, token):
            original(fields, token)
            raise TransportError("simulated_response_loss_after_real_create", outcome_unknown=True)

        self.transport.create = create_with_lost_response
        seed(self.store, self.aid, block(category="绝不新建分类"))
        unknown = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(unknown["status"], "outcome_unknown")
        self.transport.create = original
        recovered = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(recovered["segments"][0]["status"], "recovered")
        matches = self.transport.find_marker(f"[PTL:{self.aid}:fact]")
        self.assertEqual(len(matches), 1)
        self.assertIsNone(matches[0]["fields"]["一级分类"])
        self.evidence.append({"case": "real_create_simulated_response_loss_then_real_marker_recovery",
                              "activity_id": self.aid, "unknown": unknown, "recovered": recovered,
                              "matching_records": len(matches)})

    def test_live_cross_day_and_revision_retirement(self):
        a, b = timestamp("2001-01-01T23:30:00+08:00"), timestamp("2001-01-02T00:30:00+08:00")
        seed(self.store, self.aid, block([(a, b)]))
        created = self.adapter.sync(self.aid, 1, False)
        self.assertEqual(created["status"], "synced")
        self.assertEqual(len(created["segments"]), 2)
        seed(self.store, self.aid, block(status="recalled"), 2)
        retired = self.adapter.sync(self.aid, 2, False)
        self.assertEqual(retired["status"], "synced")
        for segment in retired["segments"]:
            fields = self.transport.get(segment["record_id"])["fields"]
            self.assertIsNone(fields["开始时间"])
            self.assertIsNone(fields["结束时间"])
        self.evidence.append({"case": "real_cross_day_and_retirement_without_deletion",
                              "activity_id": self.aid, "created": created, "retired": retired})


@unittest.skipUnless(os.environ.get("PTL_RUN_LIVE_BASE_TESTS") == "1", "explicit live-test switch required")
class HttpLiveBaseAdapterTests(LiveBaseAdapterTests):
    evidence = []
    evidence_file = "base-live-http-test-evidence.json"

    def setUp(self):
        super().setUp()
        from time_logger.feishu import Feishu
        cfg = json.loads(Path(os.environ["PTL_LIVE_FEISHU_CONFIG"]).read_text(encoding="utf-8-sig"))
        self.transport = HttpBaseTransport(Feishu(cfg["app_id"], cfg["app_secret"]),
                                           self.transport.base_token, self.transport.table_id, allow_formal_write=True)
        self.schema = self.transport.field_schema()
        self.adapter = BaseAdapter(self.store, self.transport, self.schema)

    def test_live_native_time_filter_positive_and_negative(self):
        if not os.environ.get("PTL_LIVE_POSITIVE_RECORD_ID") or not os.environ.get("PTL_LIVE_POSITIVE_START"):
            self.skipTest("explicit own positive-control record and start required")
        a = timestamp(os.environ["PTL_LIVE_POSITIVE_START"])
        positive = self.transport.find_overlaps(a, a + 60000)
        negative = self.transport.find_overlaps(a + 10800000, a + 10860000)
        self.assertTrue(any(r["record_id"] == os.environ["PTL_LIVE_POSITIVE_RECORD_ID"] for r in positive))
        self.assertEqual(negative, [])
        self.evidence.append({"case": "native_datetime_predicate_positive_negative_controls",
                              "positive_count": len(positive), "negative_count": len(negative)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
