"""Single-record legacy correction contracts against an isolated fake Base."""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from time_logger.base_adapter import BaseAdapter, _schema_fields
from time_logger.batch import export_batch, import_proposal
from time_logger.desktop_bridge import DesktopBridge, register_device
from time_logger.formal import FormalConflict, get_link, prepare_correction, record_fingerprint
from time_logger.proposal import InvalidProposal, validate_proposal
from time_logger.model_runner import CodexInterpreter, ModelUnavailable
from time_logger.store import Store, now_ms, packed


SCHEMA = {"fields": [
    {"name": "时间花销", "type": "text", "id": "title"},
    {"name": "备注", "type": "text", "id": "note"},
    {"name": "结果记录", "type": "text", "id": "result"},
    {"name": "开始时间", "type": "datetime", "id": "start"},
    {"name": "结束时间", "type": "datetime", "id": "end"},
    {"name": "一级分类", "type": "select", "id": "category", "options": [{"name": "学习"}, {"name": "工作"}]},
    {"name": "二级项目", "type": "select", "id": "project", "options": [{"name": "TEST_ONLY"}]},
    {"name": "持续时长（min）", "type": "formula", "id": "formula"},
]}


def ts(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


class FakeBase:
    read_only = False
    client_token_enforced = False
    def __init__(self):
        self.table_key = str(uuid.uuid4())
        self.records = {"recTESTlegacy": {"record_id": "recTESTlegacy", "fields": {
            "时间花销": "既有测试学习活动", "备注": "旧备注，必须保留", "结果记录": "原有成果",
            "开始时间": ts("2026-10-25T09:00:00+08:00"), "结束时间": ts("2026-10-25T11:00:00+08:00"),
            "一级分类": ["学习"], "二级项目": ["TEST_ONLY"], "持续时长（min）": 120}}}
        self.updates, self.creates = [], 0
        self.lose_update = False
    def field_schema(self):
        return copy.deepcopy(SCHEMA)
    def get(self, rid):
        return copy.deepcopy(self.records.get(rid))
    def update(self, rid, fields):
        self.updates.append((rid, copy.deepcopy(fields)))
        self.records[rid]["fields"].update(fields)
        if self.lose_update:
            self.lose_update = False
            raise OSError("test-only-lost-response")
    def create(self, *args):
        self.creates += 1
        raise AssertionError("legacy must never create a remote record")
    def find_marker(self, *args):
        return []


class FormalRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-formal-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3", "test-app", ["test-owner"])
        self.addCleanup(self.store.close)
        self.base = FakeBase()
        self.config = {"app_id": "test-app", "owner_open_id": "test-owner", "database": str(self.store.path),
                       "desktop_enabled": True, "parse": True, "base_token": "test-base", "table_id": "test-table",
                       "categories": ["学习", "工作"], "projects": ["TEST_ONLY"]}
        register_device(self.store, "desktop-test-device")
        self.bridge = DesktopBridge(self.store, self.config, "desktop-test-device")
        classification = mock.patch("time_logger.batch._classification_options", return_value=(self.config["categories"], self.config["projects"]))
        classification.start()
        self.addCleanup(classification.stop)

    def fingerprint(self):
        return record_fingerprint("recTESTlegacy", self.base.get("recTESTlegacy")["fields"], _schema_fields(SCHEMA))

    def prepare(self, fingerprint=None):
        return prepare_correction(self.store, self.config, "recTESTlegacy", fingerprint or self.fingerprint(), self.base)

    def submit(self, link, text="结束时间改为2026-10-25 10:30"):
        return self.bridge.handle({"version": 1, "op": "submit", "device_id": "desktop-test-device",
            "client_submission_id": str(uuid.uuid4()), "reported_at_ms": now_ms(), "text": text,
            "activity_id": link["activity_id"], "expected_revision": link["revision"]})["submission"]

    def correction(self, link, source):
        block = next(b for b in self.store.current_activities() if b["activity_id"] == link["activity_id"])
        block.pop("revision")
        block["activity_id"] = link["activity_id"]
        block["supersedes_revision"] = link["revision"]
        block["evidence_refs"].append(source["observation_id"])
        block["end"] = {"value_ms": ts("2026-10-25T10:30:00+08:00"), "kind": "explicit_user", "precision": "minute",
                        "source_id": source["observation_id"], "quote": "2026-10-25 10:30", "date_resolution": "explicit"}
        return block

    def accept(self, block):
        batch = export_batch(self.store, self.config)
        proposal = {"version": 1, "blocks": [block], "questions": [], "question_answers": []}
        result = import_proposal(self.store, batch["batch_id"], proposal, {"model": "test-only", "prompt_version": "test-formal-1.2"})
        self.assertEqual(result["activities_changed"], 1)
        return batch

    def test_prepare_is_read_only_single_source_and_idempotent(self):
        before = copy.deepcopy(self.base.records)
        link = self.prepare()
        again = self.prepare()
        self.assertEqual(link["activity_id"], again["activity_id"])
        self.assertEqual(link["revision"], again["revision"])
        self.assertEqual(self.base.records, before)
        self.assertEqual(self.base.updates, [])
        self.assertEqual(self.base.creates, 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM activities").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        block = self.store.current_activities()[0]
        self.assertEqual(block["human_intervals"], [])
        self.assertEqual(block["start"]["kind"], "formal_record")
        source = self.store.evidence(block["evidence_refs"])[0]
        self.assertEqual(source["source"], "formal_record")
        self.assertEqual(source["message_type"], "formal_record")
        self.assertIsNone(source["created_at_ms"])
        self.assertNotIn("持续时长（min）", source["formal_record"]["fields"])

    def test_prepare_stale_fingerprint_or_bad_id_does_not_link(self):
        with self.assertRaisesRegex(FormalConflict, "remote_record_changed"):
            self.prepare("0" * 64)
        with self.assertRaisesRegex(FormalConflict, "invalid_record_id"):
            prepare_correction(self.store, self.config, "../../secret", "0" * 64, self.base)
        self.assertEqual(self.store.current_activities(), [])

    def test_pipeline_preserves_formal_source_and_updates_only_original_end(self):
        link = self.prepare()
        source = self.submit(link)
        block = self.correction(link, source)
        batch = self.accept(block)
        self.assertTrue(any(e["message_type"] == "formal_record" and not e["new_for_batch"] for e in batch["evidence"]))
        before = copy.deepcopy(self.base.records["recTESTlegacy"]["fields"])
        adapter = BaseAdapter(self.store, self.base, SCHEMA)
        result = adapter.sync(link["activity_id"], 2, dry_run=False)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.base.updates, [("recTESTlegacy", {"结束时间": ts("2026-10-25T10:30:00+08:00")})])
        self.assertEqual(self.base.creates, 0)
        for name, value in before.items():
            if name != "结束时间":
                self.assertEqual(self.base.records["recTESTlegacy"]["fields"][name], value)
        self.assertEqual(adapter.sync(link["activity_id"], 1, False)["status"], "superseded")
        self.assertEqual(self.store.current_activities()[0]["human_intervals"], [])

    def test_remote_manual_change_blocks_without_overwrite(self):
        link = self.prepare()
        source = self.submit(link)
        self.accept(self.correction(link, source))
        self.base.records["recTESTlegacy"]["fields"]["备注"] = "远端新备注"
        result = BaseAdapter(self.store, self.base, SCHEMA).sync(link["activity_id"], 2, False)
        self.assertEqual(result["reason"], "remote_manual_change")
        self.assertEqual(self.base.updates, [])
        self.assertEqual(self.base.records["recTESTlegacy"]["fields"]["备注"], "远端新备注")

    def test_missing_legacy_mapping_cannot_create_replacement(self):
        link = self.prepare()
        with self.store.transaction() as db:
            db.execute("DELETE FROM base_mapping")
        result = BaseAdapter(self.store, self.base, SCHEMA).sync(link["activity_id"], 1, False)
        self.assertEqual(result["reason"], "legacy_mapping_missing_or_changed")
        self.assertEqual(self.base.creates, 0)

    def test_formal_timestamp_cannot_change_or_be_human_effort_source(self):
        link = self.prepare()
        source = self.submit(link)
        block = self.correction(link, source)
        evidence = self.store.evidence(block["evidence_refs"])
        block["start"]["value_ms"] += 60000
        with self.assertRaisesRegex(InvalidProposal, "formal_time_does_not_match_snapshot"):
            validate_proposal({"version": 1, "blocks": [block], "questions": []}, evidence, self.store.current_activities())
        block = self.correction(link, source)
        block["human_intervals"] = [{"start_ms": block["start"]["value_ms"], "end_ms": block["end"]["value_ms"],
                                      "source_id": block["start"]["source_id"], "quote": "开始时间"}]
        with self.assertRaisesRegex(InvalidProposal, "formal_snapshot_is_not_human_effort_evidence"):
            validate_proposal({"version": 1, "blocks": [block], "questions": []}, evidence, self.store.current_activities())

    def test_model_rephrasing_does_not_rename_or_clear_legacy_fields(self):
        link = self.prepare()
        source = self.submit(link)
        block = self.correction(link, source)
        block.update(name="模型自行改写的新名称", result="", category=None, project=None)
        self.accept(block)
        result = BaseAdapter(self.store, self.base, SCHEMA).sync(link["activity_id"], 2, False)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(self.base.updates[0][1], {"结束时间": ts("2026-10-25T10:30:00+08:00")})
        self.assertEqual(self.base.records["recTESTlegacy"]["fields"]["时间花销"], "既有测试学习活动")

    def test_lost_update_response_recovers_existing_row_without_new_update(self):
        link = self.prepare()
        source = self.submit(link)
        self.accept(self.correction(link, source))
        adapter = BaseAdapter(self.store, self.base, SCHEMA)
        self.base.lose_update = True
        self.assertEqual(adapter.sync(link["activity_id"], 2, False)["status"], "outcome_unknown")
        self.assertEqual(adapter.sync(link["activity_id"], 2, False)["status"], "synced")
        self.assertEqual(len(self.base.updates), 1)
        self.assertEqual(self.base.creates, 0)

    def test_latest_explicit_reselect_rebases_one_legacy_version(self):
        first = self.prepare()
        self.base.records["recTESTlegacy"]["fields"]["结束时间"] = ts("2026-10-25T12:00:00+08:00")
        second = self.prepare()
        self.assertEqual(first["activity_id"], second["activity_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.store.current_activities()[0]["end"]["value_ms"], ts("2026-10-25T12:00:00+08:00"))
        stale = self.submit(first)
        self.assertEqual(stale["stage"], "needs_review")
        self.assertEqual(self.base.updates, [])

    def test_reselect_during_unsynced_revision_does_not_rebase(self):
        link = self.prepare()
        source = self.submit(link)
        self.accept(self.correction(link, source))
        with self.assertRaisesRegex(FormalConflict, "activity_has_unsynced_revision"):
            self.prepare()

    def test_runner_accepts_only_controlled_snapshot_without_report_timestamp(self):
        self.prepare()
        block = self.store.current_activities()[0]
        source = self.store.evidence(block["evidence_refs"])[0]
        runner = CodexInterpreter()
        with mock.patch.object(runner, "_command_prefix", side_effect=ModelUnavailable("test-only-runner-boundary")):
            with self.assertRaisesRegex(ModelUnavailable, "test-only-runner-boundary"):
                runner.interpret([source], [block], [], [])
        for changes in ({"source": "desktop"}, {"message_type": "text"}, {"formal_record": {}}, {"kind": "receive"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaisesRegex(ValueError, "original created_at_ms"):
                    runner.interpret([{**source, **changes}], [block], [], [])

    def test_successive_correction_can_return_to_initial_time_without_mutating_snapshot(self):
        initial = ts("2026-10-25T10:00:00+08:00")
        self.base.records["recTESTlegacy"]["fields"]["结束时间"] = initial
        link = self.prepare()
        snapshot_fields = get_link(self.store, link["activity_id"])["initial_fields"]
        adapter = BaseAdapter(self.store, self.base, SCHEMA)
        for hour in ("11:00", "10:00"):
            source = self.submit(link, "结束时间改为2026-10-25 " + hour)
            block = self.correction(link, source)
            block["end"].update(value_ms=ts("2026-10-25T" + hour + ":00+08:00"), quote="2026-10-25 " + hour)
            self.accept(block)
            revision = link["revision"] + 1
            self.assertEqual(adapter.sync(link["activity_id"], revision, False)["status"], "synced")
            self.assertEqual(adapter.sync(link["activity_id"], revision, False)["segments"][0]["status"], "already_synced")
            link = self.prepare()
        self.assertEqual(self.base.updates, [
            ("recTESTlegacy", {"结束时间": ts("2026-10-25T11:00:00+08:00")}),
            ("recTESTlegacy", {"结束时间": initial})])
        self.assertEqual(get_link(self.store, link["activity_id"])["initial_fields"], snapshot_fields)
        self.assertEqual(self.base.creates, 0)


if __name__ == "__main__":
    unittest.main()
