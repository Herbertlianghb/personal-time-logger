"""Cross-machine contract tests with local SQLite and mocked SSH/model I/O."""
from __future__ import annotations

import copy
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from time_logger.batch import BatchConflict, StaleBatch, export_batch, import_proposal
from time_logger.model_runner import InvalidModelProposal
from time_logger.proposal import InvalidProposal
from time_logger.remote_worker import WorkerError, _remote, _single_worker, main as worker_main, run_once
from time_logger.store import Store, now_ms


def body(blocks=(), questions=(), answers=()):
    return {"version": 1, "blocks": list(blocks), "questions": list(questions), "question_answers": list(answers)}


def unknown_block(source):
    unknown = {"value_ms": None, "kind": "unknown", "precision": "unknown", "source_id": None,
               "quote": "", "date_resolution": "unknown"}
    return {"activity_id": None, "supersedes_revision": None, "name": "测试未知活动", "actor": "human",
            "role": "primary", "parent_activity_id": None, "category": None, "project": None, "status": "unknown",
            "evidence_refs": [source["id"]], "start": unknown.copy(), "end": unknown.copy(), "human_intervals": [],
            "reported_duration_seconds": None, "result": "", "uncertainty": "起止未知", "subtasks": []}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-batch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "test.sqlite3", "test-batch-app", ["test-batch-owner"], ["test-batch-chat"])
        self.addCleanup(self.store.close)
        self.config = {"parse": True, "categories": ["学习"], "projects": []}
        self.counter = 0
        self.metadata = {"model": "test-only-model", "prompt_version": "test-only-1.1.0"}

    def message(self, content="测试消息", **changes):
        self.counter += 1
        return {"app_scope": "test-batch-app", "message_id": "test-batch-" + str(self.counter),
                "chat_id": "test-batch-chat", "sender_key": "test-batch-owner", "sender_type": "user",
                "message_type": "text", "created_at_ms": now_ms(), "content": content, "kind": "receive",
                **changes}

    def put(self, content="测试消息", **changes):
        result = self.store.ingest(self.message(content, **changes))
        return self.store.evidence([result["observation_id"]])[0]

    def complete_interpret(self):
        while True:
            job = self.store.claim_job(["interpret"])
            if not job:
                break
            self.store.finish_job(job["job_id"])

    def export(self):
        return export_batch(self.store, self.config)

    def import_(self, batch, proposal, accept=True):
        return import_proposal(self.store, batch["batch_id"], proposal, self.metadata, accept)

    def seed_question(self):
        source = self.put("昨天1:30睡的，日期不清楚。")
        question = {"semantic_key": "test-date-ambiguity", "text": "1:30指哪个自然日？",
                    "ambiguity": "跨日日期会改变归属", "evidence_refs": [source["id"]]}
        pid = self.store.save_proposal(body(questions=[question]), [source], "test-only-model", "test-question-seed")
        self.store.accept_proposal(pid)
        qid = self.store.db.execute("SELECT question_id FROM questions").fetchone()[0]
        with self.store.transaction() as db:
            db.execute("UPDATE questions SET asked_at_ms=? WHERE question_id=?", (source["created_at_ms"], qid))
        self.complete_interpret()
        return qid, source

    def test_disabled_parser_does_not_claim_jobs(self):
        self.put()
        result = export_batch(self.store, {"parse": False})
        self.assertEqual(result["status"], "disabled")
        row = self.store.db.execute("SELECT status,attempts FROM jobs WHERE job_type='interpret'").fetchone()
        self.assertEqual(tuple(row), ("pending", 0))

    def test_export_uses_live_lifestyle_options_instead_of_narrow_config(self):
        self.put("现在去洗澡")
        self.config.update({"app_id": "test-app", "app_secret": "test-only-placeholder",
                            "base_token": "test-base", "time_table_id": "test-table",
                            "projects": ["单词突围老友记 S4"]})
        schema = {"fields": [
            {"name": "一级分类", "type": "select", "options": [{"name": name} for name in ("工作", "生活", "学习")]},
            {"name": "二级项目", "type": "select", "options": [{"name": name} for name in ("上厕所", "吃饭", "洗澡", "通勤", "睡觉", "单词突围老友记 S4")]},
        ]}
        with mock.patch("time_logger.base_adapter.HttpBaseTransport") as transport:
            transport.return_value.field_schema.return_value = schema
            batch = self.export()
        self.assertEqual(batch["categories"], ["工作", "生活", "学习"])
        self.assertEqual(batch["projects"], ["上厕所", "吃饭", "洗澡", "通勤", "睡觉", "单词突围老友记 S4"])
        transport.return_value.field_schema.assert_called_once_with()
        self.assertFalse(transport.call_args.kwargs.get("allow_formal_write", False))

    def test_live_option_read_failure_leaves_reports_unclaimed(self):
        self.put("现在上厕所")
        self.config.update({"app_id": "test-app", "app_secret": "test-only-placeholder",
                            "base_token": "test-base", "time_table_id": "test-table"})
        with mock.patch("time_logger.base_adapter.HttpBaseTransport") as transport:
            transport.return_value.field_schema.side_effect = RuntimeError("test-only-unavailable")
            with self.assertRaisesRegex(RuntimeError, "test-only-unavailable"):
                self.export()
        row = self.store.db.execute("SELECT status,attempts FROM jobs WHERE job_type='interpret'").fetchone()
        self.assertEqual(tuple(row), ("pending", 0))

    def test_changed_live_options_requeue_exported_bundle_without_mutating_its_hash(self):
        self.put("现在去洗澡")
        with mock.patch("time_logger.batch._classification_options", return_value=(["学习", "生活"], ["单词突围老友记 S4"])):
            first = self.export()
        with mock.patch("time_logger.batch._classification_options", return_value=(["生活", "学习"], ["单词突围老友记 S4"])):
            reordered = self.export()
        self.assertEqual(reordered["batch_id"], first["batch_id"])
        with mock.patch("time_logger.batch._classification_options", return_value=(["学习", "生活"], ["单词突围老友记 S4", "洗澡"])):
            replacement = self.export()
        self.assertNotEqual(replacement["batch_id"], first["batch_id"])
        self.assertEqual(replacement["new_evidence_ids"], first["new_evidence_ids"])
        self.assertIn("洗澡", replacement["projects"])
        stale = self.store.db.execute("SELECT status,input_hash,bundle FROM interpretation_batches WHERE batch_id=?",
                                      (first["batch_id"],)).fetchone()
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["input_hash"], first["input_hash"])
        self.assertEqual(json.loads(stale["bundle"]), first)

    def test_reported_state_changes_link_learning_to_toilet_and_resume_without_effort(self):
        def anchor(source):
            return {"value_ms": source["created_at_ms"], "kind": "report_anchor", "precision": "millisecond",
                    "source_id": source["id"], "quote": source["content"], "date_resolution": "contextual"}

        first = self.put("现在背单词", created_at_ms=1793101800000)
        learning = unknown_block(first)
        learning.update(name="背单词", category="学习", status="active", start=anchor(first))
        self.import_(self.export(), body([learning]))
        current_learning = self.store.current_activities()[0]

        switch = self.put("暂停，去上厕所", created_at_ms=1793104140000)
        paused = copy.deepcopy(learning)
        paused.update(activity_id=current_learning["activity_id"], supersedes_revision=current_learning["revision"],
                      status="paused", end=anchor(switch), evidence_refs=[first["id"], switch["id"]])
        toilet = unknown_block(switch)
        toilet.update(name="上厕所", category="生活", project="上厕所", status="active", start=anchor(switch))
        self.import_(self.export(), body([paused, toilet]))
        current_toilet = next(item for item in self.store.current_activities() if item["name"] == "上厕所")

        returned = self.put("回来了背单词", created_at_ms=1793104740000)
        ended_toilet = copy.deepcopy(toilet)
        ended_toilet.update(activity_id=current_toilet["activity_id"], supersedes_revision=current_toilet["revision"],
                            status="ended", end=anchor(returned), evidence_refs=[switch["id"], returned["id"]])
        resumed = unknown_block(returned)
        resumed.update(name="恢复背单词", category="学习", status="active", start=anchor(returned))
        result = self.import_(self.export(), body([ended_toilet, resumed]))
        self.assertEqual(result["activities_changed"], 2)
        activities = {item["name"]: item for item in self.store.current_activities()}
        self.assertEqual(len(activities), 3)
        self.assertEqual(activities["背单词"]["activity_id"], current_learning["activity_id"])
        self.assertEqual(activities["背单词"]["end"], activities["上厕所"]["start"])
        self.assertEqual(activities["上厕所"]["end"], activities["恢复背单词"]["start"])
        self.assertEqual(activities["背单词"]["status"], "paused")
        self.assertEqual(activities["上厕所"]["status"], "ended")
        self.assertIsNone(activities["恢复背单词"]["end"]["value_ms"])
        self.assertTrue(all(item["human_intervals"] == [] for item in activities.values()))

    def test_export_claims_at_most_30_and_reuses_durable_bundle(self):
        for index in range(35):
            self.put("批处理样本" + str(index))
        first = self.export()
        second = self.export()
        self.assertEqual(len(first["job_ids"]), 30)
        self.assertEqual(len(first["new_evidence_ids"]), 30)
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(len(first["evidence"]), 30)  # no future unclaimed messages as context
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='interpret' AND status='pending'").fetchone()[0], 5)
        self.assertEqual(self.store.db.execute("SELECT max(attempts) FROM jobs WHERE job_type='interpret'").fetchone()[0], 1)

    def test_trusted_test_batch_completes_before_separate_real_batch_without_inherited_flag(self):
        test_source = self.put("今天16:00学习。", message_id="om_partition_test")
        real_source = self.put("编写测试工具，实际起止未知。", message_id="om_partition_real")
        self.store.tag_test_messages([test_source["message_id"]])
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET next_at_ms=0 WHERE dedup_key=?", ("interpret:" + test_source["id"],))
            db.execute("UPDATE jobs SET next_at_ms=1 WHERE dedup_key=?", ("interpret:" + real_source["id"],))
        first = self.export()
        self.assertTrue(first["is_acceptance_test"])
        self.assertEqual(first["new_evidence_ids"], [test_source["id"]])
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE dedup_key=?", ("interpret:" + real_source["id"],)).fetchone()[0], "pending")
        question = {"semantic_key": "acceptance-test-only-question", "text": "合成问题？",
                    "ambiguity": "只供验收的歧义", "evidence_refs": [test_source["id"]]}
        self.assertEqual(self.import_(first, body([unknown_block(test_source)], [question]))["status"], "imported")
        self.assertTrue(self.store.current_activities()[0]["_acceptance_test"])

        second = self.export()
        self.assertFalse(second["is_acceptance_test"])
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(second["new_evidence_ids"], [real_source["id"]])
        self.assertEqual(second["current_blocks"], [])
        self.assertEqual(second["questions"], [])
        self.assertNotIn(test_source["id"], second["evidence_ids"])
        self.assertTrue(all(not item["is_acceptance_test"] for item in second["evidence"]))
        self.import_(second, body([unknown_block(real_source)]))
        real_activity = next(item for item in self.store.current_activities() if real_source["id"] in item["evidence_refs"])
        self.assertFalse(real_activity.get("_acceptance_test", False))
        self.assertEqual(self.export()["status"], "idle")

    def test_test_batch_never_receives_or_modifies_real_activity_context(self):
        original = self.put("真实活动原文。")
        pid = self.store.save_proposal(body([unknown_block(original)]), [original], "test-only-model", "real-seed")
        real_aid = self.store.accept_proposal(pid)[0]
        self.complete_interpret()
        test_source = self.put("新合成样本。", message_id="om_test_without_real_context")
        self.store.tag_test_messages([test_source["message_id"]])
        batch = self.export()
        self.assertTrue(batch["is_acceptance_test"])
        self.assertEqual(batch["current_blocks"], [])
        self.assertNotIn(original["id"], batch["evidence_ids"])
        forged_revision = unknown_block(test_source)
        forged_revision.update(activity_id=real_aid, supersedes_revision=1)
        with self.assertRaises(InvalidProposal):
            self.import_(batch, body([forged_revision]))
        self.assertEqual(self.store.current_activities()[0]["revision"], 1)
        self.assertEqual(self.store.current_activities()[0]["evidence_refs"], [original["id"]])

    def test_partition_keeps_other_class_pending_and_caps_selected_class_at_30(self):
        tests = [self.put("同类样本" + str(i), message_id="om_partition_" + str(i)) for i in range(31)]
        real = self.put("另一类实际报告。", message_id="om_non_test_pending")
        self.store.tag_test_messages([item["message_id"] for item in tests])
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET next_at_ms=0 WHERE job_type='interpret'")
            db.execute("UPDATE jobs SET next_at_ms=1 WHERE dedup_key=?", ("interpret:" + real["id"],))
        batch = self.export()
        self.assertTrue(batch["is_acceptance_test"])
        self.assertEqual(len(batch["job_ids"]), 30)
        self.assertTrue(all(item["is_acceptance_test"] for item in batch["evidence"]))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='interpret' AND status='pending'").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE dedup_key=?", ("interpret:" + real["id"],)).fetchone()[0], "pending")

    def test_trusted_tag_change_invalidates_exported_batch_and_repartitions(self):
        source = self.put("先导出后登记验收标识。", message_id="om_retag_during_model")
        first = self.export()
        self.assertFalse(first["is_acceptance_test"])
        self.store.tag_test_messages([source["message_id"]])
        with self.assertRaises(StaleBatch):
            self.import_(first, body([unknown_block(source)]))
        second = self.export()
        self.assertTrue(second["is_acceptance_test"])
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(second["new_evidence_ids"], [source["id"]])

    def test_superseded_observation_job_is_not_exported(self):
        first = self.put("旧原文", message_id="test-edit")
        second = self.put("新原文", message_id="test-edit", kind="edit", updated_at_ms=now_ms() + 1000)
        batch = self.export()
        self.assertEqual(batch["new_evidence_ids"], [second["id"]])
        self.assertNotIn(first["id"], batch["evidence_ids"])
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE dedup_key=?", ("interpret:" + first["id"],)).fetchone()[0], "superseded")

    def test_attachment_is_preserved_and_explicitly_deferred(self):
        source = self.put("TEST_IMAGE_REFERENCE", message_type="image")
        batch = self.export()
        self.assertIsNone(batch["batch_id"])
        self.assertEqual(batch["attachments_deferred"], 1)
        self.assertEqual(self.store.evidence([source["id"]])[0]["content"], "TEST_IMAGE_REFERENCE")
        extraction = self.store.db.execute("SELECT payload FROM jobs WHERE job_type='attachment'").fetchone()
        self.assertEqual(json.loads(extraction[0])["observation_id"], source["id"])

    def test_import_replay_is_idempotent_and_keeps_original_activity_id(self):
        source = self.put("刚刚刷手机，起止未知。")
        batch = self.export()
        proposal = body([unknown_block(source)])
        first = self.import_(batch, proposal)
        aid = self.store.current_activities()[0]["activity_id"]
        replay = self.import_(batch, proposal)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["proposal_id"], replay["proposal_id"])
        self.assertEqual(self.store.current_activities()[0]["activity_id"], aid)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM activities").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_type='interpret'").fetchone()[0], "done")
        changed = copy.deepcopy(proposal)
        changed["blocks"][0]["name"] = "重放不得改写"
        with self.assertRaises(BatchConflict):
            self.import_(batch, changed)

    def test_accept_false_is_reviewable_and_can_later_accept_once(self):
        source = self.put()
        batch = self.export()
        proposal = body([unknown_block(source)])
        candidate = self.import_(batch, proposal, accept=False)
        self.assertFalse(candidate["accepted"])
        self.assertFalse(self.store.current_activities())
        accepted = self.import_(batch, proposal)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(len(self.store.current_activities()), 1)
        self.assertTrue(self.import_(batch, proposal)["replayed"])

    def test_revision_change_rejects_batch_and_requeues(self):
        original = self.put("最初的未知活动。")
        initial = unknown_block(original)
        pid = self.store.save_proposal(body([initial]), [original], "test-only-model", "seed")
        aid = self.store.accept_proposal(pid)[0]
        self.complete_interpret()
        update = self.put("补充活动结果。")
        batch = self.export()
        revised = unknown_block(update)
        revised.update(activity_id=aid, supersedes_revision=1)
        pid = self.store.save_proposal(body([revised]), self.store.evidence(), "test-only-model", "revision")
        self.store.accept_proposal(pid)
        with self.assertRaises(StaleBatch):
            self.import_(batch, body())
        self.assertEqual(self.store.db.execute("SELECT status FROM interpretation_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0], "stale")
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE dedup_key=?", ("interpret:" + update["id"],)).fetchone()[0], "retry")

    def test_edit_during_inference_stales_snapshot_without_applying(self):
        source = self.put("原始报告", message_id="test-during-model")
        batch = self.export()
        newer = self.put("推理期间的新报告", message_id="test-during-model", kind="edit", updated_at_ms=now_ms() + 1000)
        with self.assertRaises(StaleBatch):
            self.import_(batch, body([unknown_block(source)]))
        next_batch = self.export()
        self.assertNotEqual(batch["batch_id"], next_batch["batch_id"])
        self.assertIn(newer["id"], next_batch["new_evidence_ids"])
        self.assertFalse(self.store.current_activities())

    def test_schema_failure_does_not_complete_jobs(self):
        self.put()
        batch = self.export()
        malformed = {"version": 1, "blocks": [], "questions": []}
        with self.assertRaises(InvalidModelProposal):
            self.import_(batch, malformed)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_type='interpret'").fetchone()[0], "retry")
        self.assertEqual(self.store.db.execute("SELECT status FROM interpretation_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0], "rejected")

    def test_permanent_rejection_then_next_worker_run_recovers_complete_contract(self):
        # Only SSH and model execution are replaced.  Both durable databases,
        # export/import validation, rejection, requeue and acceptance are real.
        source = self.put("起止未知的构造样本。")
        local = {"state_file": str(self.root / "contract-worker.sqlite3")}
        exported = []

        def bridge(config, action, payload=None, batch_id=None):
            if action == "export-batch":
                bundle = export_batch(self.store, self.config)
                if bundle.get("batch_id"):
                    exported.append(bundle["batch_id"])
                return bundle
            try:
                return import_proposal(self.store, batch_id, payload["proposal"], payload["metadata"])
            except (StaleBatch, BatchConflict, InvalidProposal, InvalidModelProposal) as error:
                raise WorkerError(type(error).__name__) from None

        bad = body([unknown_block(source)])
        bad["blocks"][0]["evidence_refs"] = ["not-in-exported-evidence"]
        valid = body([unknown_block(source)])
        interpreter = mock.Mock()
        interpreter.interpret.side_effect = [bad, valid]
        interpreter.last_run_metadata = self.metadata
        with mock.patch("time_logger.remote_worker._remote", side_effect=bridge), mock.patch("time_logger.remote_worker.CodexInterpreter", return_value=interpreter):
            with self.assertRaisesRegex(WorkerError, "InvalidProposal"):
                run_once(local)
            first_id = exported[0]
            self.assertEqual(self.store.db.execute("SELECT status FROM interpretation_batches WHERE batch_id=?", (first_id,)).fetchone()[0], "rejected")
            self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_type='interpret'").fetchone()[0], "retry")
            second = run_once(local)
            self.assertEqual(second["status"], "imported")
            self.assertNotEqual(first_id, exported[1])
            self.assertEqual(run_once(local)["status"], "idle")
        self.assertEqual(interpreter.interpret.call_count, 2)
        self.assertEqual(len(self.store.current_activities()), 1)
        with closing(sqlite3.connect(local["state_file"])) as db:
            self.assertEqual(dict(db.execute("SELECT batch_id,status FROM model_outbox")),
                             {first_id: "rejected", exported[1]: "imported"})
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_type='interpret'").fetchone()[0], "done")
        # Rejected input must never be able to mutate the successfully accepted
        # replacement, nor may altered replay discard that accepted batch.
        changed = copy.deepcopy(valid)
        changed["blocks"][0]["name"] = "不得重写已接受事实"
        accepted_id = exported[1]
        with self.assertRaises(BatchConflict):
            import_proposal(self.store, accepted_id, changed, self.metadata)
        self.assertEqual(self.store.db.execute("SELECT status FROM interpretation_batches WHERE batch_id=?", (accepted_id,)).fetchone()[0], "imported")
        self.assertEqual(len(self.store.current_activities()), 1)
        self.assertEqual(export_batch(self.store, self.config)["status"], "idle")

    def test_schema_and_hash_rejections_release_unaccepted_batch(self):
        source = self.put("验证拒绝后的重新导出。")
        for error_type in (InvalidModelProposal, BatchConflict):
            with self.subTest(error_type=error_type.__name__):
                batch = self.export()
                metadata = dict(self.metadata)
                proposal = body([unknown_block(source)])
                if error_type is InvalidModelProposal:
                    proposal.pop("question_answers")
                else:
                    metadata["batch_input_hash"] = "mismatched-hash"
                with self.assertRaises(error_type):
                    import_proposal(self.store, batch["batch_id"], proposal, metadata)
                self.assertEqual(self.store.db.execute("SELECT status FROM interpretation_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0], "rejected")
                self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_type='interpret'").fetchone()[0], "retry")
        replacement = self.export()
        self.assertNotEqual(replacement["batch_id"], batch["batch_id"])
        self.assertEqual(self.import_(replacement, body([unknown_block(source)]))["status"], "imported")

    def test_rejected_bundle_reexport_does_not_rerun_model_or_reinsert_key(self):
        source = self.put("旧服务重复导出已拒绝包的保护。")
        bundle = self.export()
        invalid = body([unknown_block(source)])
        invalid["blocks"][0]["evidence_refs"] = ["invalid-reference"]
        interpreter = mock.Mock()
        interpreter.interpret.return_value = invalid
        interpreter.last_run_metadata = self.metadata
        local = {"state_file": str(self.root / "old-server-worker.sqlite3")}

        def bridge(config, action, payload=None, batch_id=None):
            if action == "export-batch":
                return bundle  # an old server incorrectly reexports rejected ID
            try:
                return import_proposal(self.store, batch_id, payload["proposal"], payload["metadata"])
            except InvalidProposal:
                raise WorkerError("InvalidProposal") from None

        with mock.patch("time_logger.remote_worker._remote", side_effect=bridge), mock.patch("time_logger.remote_worker.CodexInterpreter", return_value=interpreter):
            with self.assertRaisesRegex(WorkerError, "InvalidProposal"):
                run_once(local)
            with self.assertRaisesRegex(WorkerError, "BatchConflict"):
                run_once(local)
        self.assertEqual(interpreter.interpret.call_count, 1)
        with closing(sqlite3.connect(local["state_file"])) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM model_outbox").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM model_outbox").fetchone()[0], "rejected")

    def test_reexport_discards_edited_snapshot_before_spending_model_quota(self):
        self.put("旧报告", message_id="test-before-export")
        first = self.export()
        newer = self.put("已编辑的新报告", message_id="test-before-export", kind="edit", updated_at_ms=now_ms() + 1000)
        second = self.export()
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(second["new_evidence_ids"], [newer["id"]])

    def test_context_only_evidence_cannot_create_duplicate_activity(self):
        old = self.put("已经整理过的原文。")
        pid = self.store.save_proposal(body([unknown_block(old)]), [old], "test-only-model", "seed")
        self.store.accept_proposal(pid)
        self.complete_interpret()
        self.put("本批新报告。")
        batch = self.export()
        self.assertTrue(any(item["id"] == old["id"] and not item["new_for_batch"] for item in batch["evidence"]))
        with self.assertRaises(InvalidProposal):
            self.import_(batch, body([unknown_block(old)]))
        self.assertEqual(len(self.store.current_activities()), 1)

    def test_natural_skip_answer_is_persistent_and_not_reopened(self):
        qid, _ = self.seed_question()
        answer = self.put("不知道，先不管。")
        batch = self.export()
        proposal = body(answers=[{"question_id": qid, "answer_evidence_id": answer["id"], "disposition": "skipped"}])
        result = self.import_(batch, proposal)
        self.assertEqual(result["answers_saved"], 1)
        row = self.store.db.execute("SELECT status,answer_observation_id FROM questions WHERE question_id=?", (qid,)).fetchone()
        self.assertEqual(tuple(row), ("skipped", answer["id"]))
        self.assertTrue(self.import_(batch, proposal)["replayed"])
        self.put("另一个新的活动报告。")
        next_batch = self.export()
        self.assertEqual(next(item for item in next_batch["questions"] if item["question_id"] == qid)["status"], "skipped")

    def test_old_context_cannot_be_used_as_new_question_answer(self):
        qid, old = self.seed_question()
        self.put("新的无关报告。")
        batch = self.export()
        wrong = body(answers=[{"question_id": qid, "answer_evidence_id": old["id"], "disposition": "answered"}])
        with self.assertRaises(InvalidProposal):
            self.import_(batch, wrong)
        self.assertEqual(self.store.db.execute("SELECT status FROM questions WHERE question_id=?", (qid,)).fetchone()[0], "pending")

    def test_unknown_question_cannot_be_answered(self):
        source = self.put("回答一个不存在的问题。")
        batch = self.export()
        wrong = body(answers=[{"question_id": "test-unknown-question", "answer_evidence_id": source["id"], "disposition": "answered"}])
        with self.assertRaises(InvalidProposal):
            self.import_(batch, wrong)

    def test_atomic_import_rolls_back_activity_when_answer_save_fails(self):
        qid, _ = self.seed_question()
        answer = self.put("不知道，顺便补记刚才刷手机。")
        batch = self.export()
        proposal = body([unknown_block(answer)], answers=[{"question_id": qid, "answer_evidence_id": answer["id"], "disposition": "skipped"}])
        before = self.store.db.execute("SELECT count(*) FROM proposals").fetchone()[0]
        with mock.patch.object(self.store, "resolve_question", side_effect=RuntimeError("test-only failure")):
            with self.assertRaises(RuntimeError):
                self.import_(batch, proposal)
        self.assertFalse(self.store.current_activities())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM proposals").fetchone()[0], before)
        self.assertEqual(self.store.db.execute("SELECT status FROM interpretation_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0], "exported")
        self.assertEqual(self.import_(batch, proposal)["activities_changed"], 1)

    def test_remote_worker_keeps_text_out_of_returned_summary(self):
        bundle = {"batch_id": "f4e3acf9-f8b2-4f45-a2d8-e233a8788eed", "evidence": [{"content": "TEST_PRIVATE_TEXT"}],
                  "current_blocks": [], "categories": [], "projects": [], "questions": [], "input_hash": "test-hash"}
        interpreter = mock.Mock()
        interpreter.interpret.return_value = body()
        interpreter.last_run_metadata = self.metadata
        with mock.patch("time_logger.remote_worker._remote", side_effect=[bundle, {"status": "imported", "processed": 1, "proposal_id": "private-id"}]) as remote, mock.patch("time_logger.remote_worker.CodexInterpreter", return_value=interpreter):
            result = run_once({})
        self.assertEqual(result, {"status": "imported", "processed": 1})
        self.assertNotIn("TEST_PRIVATE_TEXT", json.dumps(result))
        self.assertEqual(remote.call_args.args[2]["metadata"]["batch_input_hash"], "test-hash")

    def test_local_outbox_retries_import_without_calling_model_again(self):
        bundle = {"batch_id": "f4e3acf9-f8b2-4f45-a2d8-e233a8788eed", "evidence": [], "current_blocks": [],
                  "categories": [], "projects": [], "questions": [], "input_hash": "test-hash"}
        interpreter = mock.Mock()
        interpreter.interpret.return_value = body()
        interpreter.last_run_metadata = self.metadata
        config = {"state_file": str(self.root / "local-state.sqlite3")}
        with mock.patch("time_logger.remote_worker._remote", side_effect=[bundle, WorkerError("remote_operation_failed")]), mock.patch("time_logger.remote_worker.CodexInterpreter", return_value=interpreter):
            with self.assertRaises(WorkerError):
                run_once(config)
        with mock.patch("time_logger.remote_worker._remote", return_value={"status": "imported", "processed": 1, "replayed": True}) as remote, mock.patch("time_logger.remote_worker.CodexInterpreter") as model:
            result = run_once(config)
        model.assert_not_called()
        self.assertEqual(remote.call_args.args[1], "import-proposal")
        self.assertTrue(result["replayed"])

    def test_local_outbox_discards_stale_candidate_then_exports_fresh_batch(self):
        bundle = {"batch_id": "f4e3acf9-f8b2-4f45-a2d8-e233a8788eed", "evidence": [], "current_blocks": [],
                  "categories": [], "projects": [], "questions": [], "input_hash": "test-hash"}
        interpreter = mock.Mock()
        interpreter.interpret.return_value = body()
        interpreter.last_run_metadata = self.metadata
        config = {"state_file": str(self.root / "local-state.sqlite3")}
        with mock.patch("time_logger.remote_worker._remote", side_effect=[bundle, WorkerError("StaleBatch")]), mock.patch("time_logger.remote_worker.CodexInterpreter", return_value=interpreter):
            with self.assertRaises(WorkerError):
                run_once(config)
        with mock.patch("time_logger.remote_worker._remote", return_value={"status": "idle", "batch_id": None}) as remote:
            result = run_once(config)
        self.assertEqual(remote.call_args.args[1], "export-batch")
        self.assertEqual(result["status"], "idle")

    def test_ssh_structured_stale_rejection_is_classified_without_raw_output(self):
        config_file = self.root / "ssh-config"
        config_file.write_text("Host personal-time-logger\n", encoding="utf-8")
        result = subprocess.CompletedProcess([], 1, '{"status":"rejected","error_class":"StaleBatch"}', "TEST_PRIVATE_SECRET")
        with mock.patch("time_logger.remote_worker.shutil.which", return_value="test-ssh.exe"), mock.patch("time_logger.remote_worker.subprocess.run", return_value=result):
            with self.assertRaises(WorkerError) as caught:
                _remote({"ssh_config": str(config_file)}, "export-batch")
        self.assertEqual(str(caught.exception), "StaleBatch")

    def test_ssh_uses_config_and_stdin_without_shell_or_private_command_text(self):
        config_file = self.root / "private ssh config"
        config_file.write_text("Host personal-time-logger\n  HostName example.invalid\n", encoding="utf-8")
        config = {"ssh_config": str(config_file), "remote_cwd": "/opt/project with spaces"}
        result = subprocess.CompletedProcess([], 0, '{"status":"imported","processed":1}', "")
        with mock.patch("time_logger.remote_worker.shutil.which", return_value="test-ssh.exe"), mock.patch("time_logger.remote_worker.subprocess.run", return_value=result) as execute:
            _remote(config, "import-proposal", {"text": "TEST_PRIVATE_PAYLOAD"}, "f4e3acf9-f8b2-4f45-a2d8-e233a8788eed")
        args, kwargs = execute.call_args
        self.assertFalse(kwargs["shell"])
        self.assertIn("-F", args[0])
        self.assertNotIn("TEST_PRIVATE_PAYLOAD", " ".join(args[0]))
        self.assertIn("TEST_PRIVATE_PAYLOAD", kwargs["input"])
        self.assertIn("cd '/opt/project with spaces'", args[0][-1])

    def test_remote_error_never_echoes_private_stderr(self):
        config_file = self.root / "ssh-config"
        config_file.write_text("Host personal-time-logger\n", encoding="utf-8")
        result = subprocess.CompletedProcess([], 1, "TEST_PRIVATE_STDOUT", "TEST_PRIVATE_SECRET")
        with mock.patch("time_logger.remote_worker.shutil.which", return_value="test-ssh.exe"), mock.patch("time_logger.remote_worker.subprocess.run", return_value=result):
            with self.assertRaises(WorkerError) as caught:
                _remote({"ssh_config": str(config_file)}, "export-batch")
        self.assertEqual(str(caught.exception), "remote_operation_failed")

    def test_worker_lock_prevents_concurrent_worker_instances_and_releases(self):
        config_path = self.root / "worker-config.json"
        with _single_worker(config_path):
            with self.assertRaises(WorkerError):
                with _single_worker(config_path):
                    self.fail("concurrent worker entered")
        with _single_worker(config_path):
            pass

    def test_worker_database_failure_only_outputs_error_class(self):
        config_path = self.root / "worker-config.json"
        config_path.write_text('{}', encoding="utf-8")
        output = io.StringIO()
        with mock.patch("sys.argv", ["remote_worker", "--config", str(config_path)]), mock.patch("sys.stdout", output), mock.patch("time_logger.remote_worker.run_once", side_effect=sqlite3.DatabaseError("TEST_PRIVATE_DIAGNOSTIC")):
            code = worker_main()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "deferred", "processed": 0, "error_class": "DatabaseError"})


if __name__ == "__main__":
    unittest.main()
