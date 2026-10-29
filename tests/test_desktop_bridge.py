"""Isolated desktop bridge contracts; no live credentials or Base writes."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import uuid

from time_logger.batch import export_batch
from time_logger.desktop_bridge import BridgeError, DesktopBridge, MAX_REQUEST_BYTES, read_request, register_device
from time_logger.service import Service
from time_logger.store import Store, now_ms, packed


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-desktop-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "test.sqlite3", "app-test", ["owner-test"], ["chat-test"])
        self.addCleanup(self.store.close)
        self.config = {"database": str(self.root / "test.sqlite3"), "app_id": "app-test", "owner_open_id": "owner-test",
                       "chat_id": "chat-test", "desktop_enabled": True, "ingest": True, "parse": True,
                       "categories": ["学习"], "projects": []}
        self.device = "desktop-test-device"
        register_device(self.store, self.device)
        self.bridge = DesktopBridge(self.store, self.config, self.device)

    def request(self, op="submit", **changes):
        value = {"version": 1, "op": op, "device_id": self.device}
        if op in {"submit", "question_answer"}:
            value.update(client_submission_id=str(uuid.uuid4()), reported_at_ms=now_ms(), text="测试原文，不是真实用户活动")
        return {**value, **changes}

    def seed_activity(self, aid="old-activity", revision=1, name="测试活动"):
        unknown = {"value_ms": None, "kind": "unknown", "precision": "unknown", "source_id": None,
                   "quote": "", "date_resolution": "unknown"}
        block = {"activity_id": None, "supersedes_revision": None, "name": name, "actor": "human", "role": "primary",
                 "parent_activity_id": None, "category": None, "project": None, "status": "unknown", "evidence_refs": [],
                 "start": unknown.copy(), "end": unknown.copy(), "human_intervals": [], "reported_duration_seconds": None,
                 "result": "", "uncertainty": "", "subtasks": []}
        with self.store.transaction() as db:
            db.execute("UPDATE activities SET current_valid=0 WHERE activity_id=?", (aid,))
            db.execute("INSERT INTO activities VALUES(?,?,?,?,1,?)", (aid, revision, "test-only-proposal", packed(block), now_ms()))
        return block

    def seed_question(self, suffix="1", asked=True):
        source = self.bridge.handle(self.request())["submission"]["observation_id"]
        q = {"semantic_key": "question-" + suffix, "text": "测试问题" + suffix, "ambiguity": "日期", "evidence_refs": [source]}
        with self.store.transaction() as db:
            db.execute("INSERT INTO questions(question_id,semantic_key,proposal_id,body,asked_at_ms) VALUES(?,?,?,?,?)",
                       ("qid-" + suffix, q["semantic_key"], "test-proposal", packed(q), now_ms() if asked else None))
        return "qid-" + suffix

    def test_submit_commits_same_pipeline_preserves_raw_and_has_no_im_receipt(self):
        request = self.request(text="第一行\n第二行  ")
        result = self.bridge.handle(request)
        self.assertTrue(result["ok"])
        self.assertFalse(result["duplicate"])
        submission = result["submission"]
        self.assertEqual(submission["stage"], "server_saved")
        evidence = self.store.evidence([submission["observation_id"]])[0]
        self.assertEqual(evidence["content"], request["text"])
        self.assertEqual(evidence["source"], "desktop")
        self.assertTrue(evidence["message_id"].startswith("desktop:"))
        self.assertFalse(evidence["message_id"].startswith("om_"))
        self.assertEqual(evidence["desktop_context"]["device_reported_at_ms"], request["reported_at_ms"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='interpret'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='receipt'").fetchone()[0], 0)
        # A distinct Store connection sees the committed transaction.
        with mock.patch.object(self.store, "close"):
            other = Store(self.root / "test.sqlite3", "app-test", ["owner-test"])
            try:
                self.assertEqual(other.evidence([submission["observation_id"]])[0]["content"], request["text"])
            finally:
                other.close()

    def test_retry_is_idempotent_and_conflicting_payload_is_rejected(self):
        request = self.request()
        first = self.bridge.handle(request)
        second = self.bridge.handle(request)
        self.assertEqual(first["submission"]["observation_id"], second["submission"]["observation_id"])
        self.assertTrue(second["duplicate"])
        with self.assertRaisesRegex(BridgeError, "submission_id_payload_conflict"):
            self.bridge.handle({**request, "text": "变更原文"})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 1)

    def test_transaction_failure_rolls_back_raw_job_and_mapping(self):
        original = self.store.ingest
        def fail_after_ingest(message):
            original(message)
            raise OSError("test-only-disk-error")
        with mock.patch.object(self.store, "ingest", side_effect=fail_after_ingest):
            with self.assertRaises(OSError):
                self.bridge.handle(self.request())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM desktop_submissions").fetchone()[0], 0)

    def test_source_time_not_replaced_after_offline_delay_or_bad_device_clock(self):
        original_report = now_ms() - 3 * 86400000
        submission = self.bridge.handle(self.request(reported_at_ms=original_report))["submission"]
        self.assertEqual(submission["reported_at_ms"], original_report)
        self.assertEqual(submission["report_time_trust"], "device_clock_unverified")
        self.assertGreater(submission["server_saved_at_ms"], original_report)
        self.assertEqual(self.store.evidence([submission["observation_id"]])[0]["created_at_ms"], original_report)

    def test_disabled_revoked_wrong_device_and_forbidden_operation_reject(self):
        for request, error in [(self.request(device_id="other-device"), "device_mismatch"),
                               (self.request(op="register_device"), "operation_not_allowed")]:
            with self.assertRaisesRegex(BridgeError, error):
                self.bridge.handle(request)
        self.config["desktop_enabled"] = False
        with self.assertRaisesRegex(BridgeError, "desktop_disabled"):
            self.bridge.handle(self.request())
        self.config["desktop_enabled"] = True
        register_device(self.store, self.device, False)
        with self.assertRaisesRegex(BridgeError, "device_revoked"):
            self.bridge.handle(self.request())

    def test_invalid_types_ids_text_and_timestamp_reject_without_write(self):
        for changes in ({"client_submission_id": "../../secret"}, {"reported_at_ms": True},
                        {"text": ""}, {"text": "\x00"}, {"text": "字" * 6000}, {"version": True}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(BridgeError):
                    self.bridge.handle(self.request(**changes))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_reader_bounds_and_rejects_invalid_json_and_unterminated(self):
        for value in (b"{}", b"{" + b"x" * MAX_REQUEST_BYTES + b"}\n", b"\xff\n", b"bad\n"):
            with self.assertRaises(BridgeError):
                read_request(io.BytesIO(value))
        self.assertEqual(read_request(io.BytesIO(b'{"version":1}\n')), {"version": 1})

    def test_status_is_per_device_and_uses_stable_source_id(self):
        request = self.request()
        submission = self.bridge.handle(request)["submission"]
        status = self.bridge.handle(self.request("status", client_submission_id=request["client_submission_id"]))
        self.assertEqual(status["submissions"][0]["observation_id"], submission["observation_id"])
        other_id = "another-desktop"
        register_device(self.store, other_id)
        other = DesktopBridge(self.store, self.config, other_id)
        result = other.handle({"version": 1, "op": "status", "device_id": other_id, "client_submission_id": request["client_submission_id"]})
        self.assertEqual(result["submissions"], [])
        self.assertEqual(result["not_found"], [request["client_submission_id"]])

    def test_mobile_and_desktop_pending_use_one_query_and_no_effort(self):
        self.bridge.handle(self.request())
        self.store.ingest({"app_scope": "app-test", "message_id": "om_test_mobile", "sender_key": "owner-test", "chat_id": "chat-test",
                           "created_at_ms": now_ms(), "content": '{"text":"手机受控测试"}', "source": "ws"})
        pending = self.bridge.handle(self.request("snapshot"))["pending"]
        self.assertEqual({item["source"] for item in pending}, {"desktop", "feishu"})
        self.assertTrue(all(item["stage"] == "server_saved" for item in pending))
        self.assertNotIn("human_seconds", pending[0])

    def test_formal_readback_changes_stage_and_removes_pending_without_new_event(self):
        request = self.request()
        submission = self.bridge.handle(request)["submission"]
        oid = submission["observation_id"]
        block = self.seed_activity()
        block["evidence_refs"] = [oid]
        with self.store.transaction() as db:
            db.execute("UPDATE activities SET body=? WHERE activity_id='old-activity'", (packed(block),))
            db.execute("UPDATE jobs SET status='done' WHERE dedup_key=?", ("interpret:" + oid,))
        self.assertEqual(self.bridge.handle(self.request("status", client_submission_id=request["client_submission_id"]))["submissions"][0]["stage"], "organized")
        with self.store.transaction() as db:
            db.execute("INSERT INTO base_mapping VALUES('old-activity','main','rec-test',1,'{}','schema-test')")
        current = self.bridge.handle(self.request("status", client_submission_id=request["client_submission_id"]))["submissions"][0]
        self.assertEqual(current["stage"], "archived")
        self.assertEqual(current["record_ids"], ["rec-test"])
        self.assertEqual(current["base_verification"], "previous_readback")
        self.assertEqual(self.bridge.handle(self.request("pending"))["pending"], [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_nonbase_ai_and_detail_do_not_hold_human_in_permanent_pending(self):
        request = self.request()
        oid = self.bridge.handle(request)["submission"]["observation_id"]
        for aid, actor, role in (("main", "human", "primary"), ("ai", "ai", "primary"), ("detail", "human", "detail")):
            block = self.seed_activity(aid)
            block.update(actor=actor, role=role, evidence_refs=[oid])
            with self.store.transaction() as db:
                db.execute("UPDATE activities SET body=? WHERE activity_id=?", (packed(block), aid))
        with self.store.transaction() as db:
            db.execute("INSERT INTO base_mapping VALUES('main','main','rec-test',1,'{}','schema-test')")
        current = self.bridge.handle(self.request("status", client_submission_id=request["client_submission_id"]))["submissions"][0]
        self.assertTrue(current["base_required"])
        self.assertEqual(current["stage"], "archived")
        self.assertEqual(self.bridge.handle(self.request("pending"))["pending"], [])

    def test_pure_ai_is_organized_without_claiming_base_verified(self):
        request = self.request()
        oid = self.bridge.handle(request)["submission"]["observation_id"]
        block = self.seed_activity("ai")
        block.update(actor="ai", evidence_refs=[oid])
        with self.store.transaction() as db:
            db.execute("UPDATE activities SET body=? WHERE activity_id='ai'", (packed(block),))
            db.execute("UPDATE jobs SET status='done' WHERE dedup_key=?", ("interpret:" + oid,))
        current = self.bridge.handle(self.request("status", client_submission_id=request["client_submission_id"]))["submissions"][0]
        self.assertEqual(current["stage"], "organized")
        self.assertFalse(current["base_required"])
        self.assertIsNone(current["base_verification"])
        self.assertEqual(self.bridge.handle(self.request("pending"))["pending"], [])

    def test_targeted_old_activity_is_exported_and_raw_is_not_prefixed(self):
        self.seed_activity()
        for index in range(35):
            self.seed_activity("new-activity-" + str(index))
        request = self.request(text="上一条结束时间错了", activity_id="old-activity", expected_revision=1)
        submission = self.bridge.handle(request)["submission"]
        batch = export_batch(self.store, self.config)
        self.assertIn("old-activity", batch["revisions"])
        evidence = next(item for item in batch["evidence"] if item["id"] == submission["observation_id"])
        self.assertEqual(evidence["content"], request["text"])
        self.assertEqual(evidence["desktop_context"]["target_activity_id"], "old-activity")

    def test_old_correction_is_preserved_but_cannot_overwrite_new_revision(self):
        self.seed_activity()
        request = self.request(activity_id="old-activity", expected_revision=1)
        self.bridge.handle(request)
        self.seed_activity(revision=2)
        self.assertEqual(export_batch(self.store, self.config)["status"], "idle")
        current = self.bridge.handle(self.request("status", client_submission_id=request["client_submission_id"]))["submissions"][0]
        self.assertEqual(current["stage"], "needs_review")
        self.assertEqual(current["text"], request["text"])
        self.assertEqual(current["processing_error"], "desktop_target_revision_changed")

    def test_already_stale_correction_saved_needs_review_on_first_submit(self):
        self.seed_activity(revision=2)
        current = self.bridge.handle(self.request(activity_id="old-activity", expected_revision=1))["submission"]
        self.assertEqual(current["stage"], "needs_review")
        self.assertEqual(current["context"]["current_revision_at_submit"], 2)

    def test_only_budgeted_pending_question_displayed_and_answer_resolves_once(self):
        qid = self.seed_question()
        self.seed_question("unasked", asked=False)
        questions = self.bridge.handle(self.request("questions"))["questions"]
        self.assertEqual([q["question_id"] for q in questions], [qid])
        request = self.request("question_answer", question_id=qid, text="不知道", disposition="skipped")
        first = self.bridge.handle(request)["submission"]
        self.assertEqual(self.store.db.execute("SELECT status FROM questions WHERE question_id=?", (qid,)).fetchone()[0], "skipped")
        self.assertEqual(self.bridge.handle(self.request("questions"))["questions"], [])
        self.assertEqual(self.bridge.handle(request)["submission"]["observation_id"], first["observation_id"])
        context = self.store.evidence([first["observation_id"]])[0]["desktop_context"]
        self.assertEqual(context["question_id"], qid)
        self.assertEqual(context["question_disposition"], "skipped")

    def test_phone_answer_before_desktop_upload_does_not_reopen_question_or_lose_text(self):
        qid = self.seed_question()
        answer = self.bridge.handle(self.request())["submission"]["observation_id"]
        self.store.resolve_question(qid, answer)
        result = self.bridge.handle(self.request("question_answer", question_id=qid, text="离线时写下的回答"))["submission"]
        self.assertEqual(result["text"], "离线时写下的回答")
        row = self.store.db.execute("SELECT status,answer_observation_id FROM questions WHERE question_id=?", (qid,)).fetchone()
        self.assertEqual(tuple(row), ("answered", answer))

    def test_explicit_empty_skip_preserves_action_without_model_or_activity(self):
        qid = self.seed_question()
        result = self.bridge.handle(self.request("question_answer", question_id=qid, disposition="skipped", text=""))["submission"]
        self.assertEqual(result["text"], "")
        self.assertEqual(result["stage"], "organized")
        self.assertEqual(result["activity_ids"], [])
        self.assertEqual(self.store.db.execute("SELECT status FROM questions WHERE question_id=?", (qid,)).fetchone()[0], "skipped")
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE dedup_key=?", ("interpret:" + result["observation_id"],)).fetchone()[0], "done")
        self.assertEqual(self.store.evidence([result["observation_id"]])[0]["content"], "")

    def test_resolved_queued_question_is_not_sent_again(self):
        qid = self.seed_question()
        answer = self.bridge.handle(self.request("question_answer", question_id=qid, text="不知道", disposition="skipped"))["submission"]
        with self.store.transaction() as db:
            self.store.enqueue(db, "question", "question-test", {"question_ids": [qid], "chat_id": "chat-test", "created_at_ms": now_ms(), "text": "旧问题"})
        job = self.store.claim_job(["question"])
        service = object.__new__(Service)
        service.store = self.store
        service.platform = mock.Mock()
        service.process_question(job)
        service.platform.receipt.assert_not_called()
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0], "superseded")

    def test_partially_answered_question_round_only_sends_pending_question(self):
        first, second = self.seed_question("1"), self.seed_question("2")
        self.bridge.handle(self.request("question_answer", question_id=first, text="不知道", disposition="skipped"))
        with self.store.transaction() as db:
            self.store.enqueue(db, "question", "question-test-two", {"question_ids": [first, second], "chat_id": "chat-test", "created_at_ms": now_ms(), "text": "旧两问题"})
        job = self.store.claim_job(["question"])
        service = object.__new__(Service)
        service.store = self.store
        service.platform = mock.Mock()
        service.platform.receipt.return_value = "om-test-receipt"
        service.process_question(job)
        text = service.platform.receipt.call_args.kwargs["text"]
        self.assertNotIn("测试问题1", text)
        self.assertIn("测试问题2", text)

    def test_history_passes_only_valid_bounded_parameters(self):
        fake = types.ModuleType("time_logger.timeline")
        fake.timeline_history = mock.Mock(return_value={"date": "2026-10-01", "items": [], "summary": {}})
        with mock.patch.dict("sys.modules", {"time_logger.timeline": fake}):
            result = self.bridge.handle(self.request("history", date="2026-10-01"))
        self.assertTrue(result["ok"])
        fake.timeline_history.assert_called_once_with(self.store, self.config, "2026-10-01", None, 100, refresh=False)
        for changes in ({"date": "bad"}, {"date": "2026-10-01", "limit": 101}, {"date": "2026-10-01", "refresh": "yes"}):
            with self.assertRaises(BridgeError):
                self.bridge.handle(self.request("history", **changes))


if __name__ == "__main__":
    unittest.main()
