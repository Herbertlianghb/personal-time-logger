import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from time_logger.desktop_controller import DesktopController, LOCAL_TZ

APP = QApplication.instance() or QApplication([])


class FakeTransport:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []
        self.controller = None
        self.closed = False
        self.on_request = None

    def request(self, op, **kwargs):
        self.calls.append((op, kwargs))
        if self.on_request:
            self.on_request(op, kwargs)
        expected, value = self.actions.pop(0)
        if not self.actions:
            self.controller.closed.set()
            self.controller.wakeup.set()
        if op != expected:
            raise AssertionError((op, expected))
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "local.sqlite3"
        self.transport = FakeTransport([])
        self.controller = DesktopController({}, self.path, self.transport)
        self.transport.controller = self.controller
        self.states, self.snapshots, self.pending = [], [], []
        self.controller.state.connect(self.states.append)
        self.controller.snapshot.connect(self.snapshots.append)
        self.controller.pending.connect(self.pending.append)

    def tearDown(self):
        self.controller.stop()
        self.temp.cleanup()

    def test_submit_commits_local_before_saved_signal(self):
        saved = []
        self.controller.state.connect(lambda text: saved.append(self.controller.local.queued()))
        self.controller.submit("actual text")
        self.assertEqual(self.states, ["已存本机，待上传"])
        self.assertEqual(saved[0][0]["text"], "actual text")
        self.assertEqual(self.pending[0][0]["stage"], "local_saved")

    def test_hidden_window_uploads_but_does_not_poll_history(self):
        self.controller.submit("queued")
        self.transport.actions = [("submit", {"server_now_ms": 1000, "submission": {"stage": "server_saved", "observation_id": "stable"}})]
        self.controller._run()
        self.assertEqual([op for op, kwargs in self.transport.calls], ["submit"])
        self.assertEqual(self.controller.local.list_submissions()[0]["stage"], "server_saved")

    def test_visible_refresh_reads_snapshot_and_history_only(self):
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        snap = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 1000, "data_revision": "formal", "activities": [], "summary": {"human_seconds": None}}
        self.transport.actions = [("snapshot", {"submissions": [], "pending": [], "questions": []}), ("history", {"history": snap})]
        self.controller._run()
        self.assertEqual([op for op, kwargs in self.transport.calls], ["snapshot", "history"])
        self.assertEqual(self.snapshots, [snap])
        self.assertIsNone(self.snapshots[0]["summary"]["human_seconds"])

    def test_response_loss_preserves_original_id_and_report_time(self):
        self.controller.submit("once")
        before = self.controller.local.queued()[0]
        self.transport.actions = [("submit", OSError("response_lost"))]
        self.controller._run()
        self.assertEqual(self.controller.local.queued(), [before])
        self.assertIn("同步受阻", self.states[-1])

    def test_failed_refresh_keeps_cached_snapshot_and_labels_it_stale(self):
        good = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 1000, "activities": [{"id": "formal"}], "summary": {"human_seconds": None}}
        self.controller.local.save_history(good)
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        self.transport.actions = [("snapshot", OSError("expired"))]
        self.controller._run()
        self.assertEqual(self.controller.local.cache("2026-10-26", 7), good)
        self.assertTrue(self.snapshots[-1]["cache"]["stale"])
        self.assertTrue(self.snapshots[-1]["cache"]["local_cached"])

    def test_question_id_and_activity_revision_are_durable_context(self):
        self.controller.last_snapshot = {"activities": [{"activity_id": "activity", "revision": 4}]}
        self.controller.submit("correct", activity_id="activity")
        self.controller.submit("", question_id="question", answer_action="skip")
        first, second = self.controller.local.queued()
        self.assertEqual((first["activity_id"], first["expected_revision"]), ("activity", 4))
        self.assertEqual((second["question_id"], second["disposition"]), ("question", "skipped"))

    def test_paginated_history_keeps_same_version_and_whole_day_summary(self):
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        first = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 1000, "data_revision": "same", "items": [{"id": "one"}], "summary": {"human_seconds": 10}, "next_cursor": "next"}
        second = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 1000, "data_revision": "same", "items": [{"id": "two"}], "next_cursor": None}
        self.transport.actions = [("snapshot", {"submissions": [], "pending": [], "questions": []}), ("history", {"history": first}), ("history", {"history": second})]
        self.controller._run()
        self.assertEqual([item["id"] for item in self.snapshots[-1]["activities"]], ["one", "two"])
        self.assertEqual(self.snapshots[-1]["summary"]["human_seconds"], 10)

    def test_pagination_version_change_never_replaces_good_cache(self):
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        good = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 500, "data_revision": "old-good", "activities": [{"id": "cached"}]}
        self.controller.local.save_history(good)
        first = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 1000, "data_revision": "same", "items": [{"id": "one"}], "next_cursor": "next"}
        second = {"day_start_hour": 7, "date": "2026-10-26", "as_of_ms": 1001, "data_revision": "different", "items": [{"id": "two"}]}
        self.transport.actions = [("snapshot", {"submissions": [], "pending": [], "questions": []}), ("history", {"history": first}), ("history", {"history": second})]
        self.controller._run()
        self.assertEqual(self.controller.local.cache("2026-10-26", 7), good)
        self.assertEqual(self.snapshots[-1]["data_revision"], "old-good")

    def test_refresh_preserves_latest_server_save_receipt(self):
        self.controller.submit("saved")
        body = self.controller.local.queued()[0]
        sid = body["client_submission_id"]
        self.controller.local.acknowledge(sid, {"stage": "server_saved", "observation_id": "stable"})
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        history = {"day_start_hour": 7, "date": self.controller.day, "as_of_ms": 1000, "activities": [], "summary": {"human_seconds": None}}
        self.transport.actions = [("snapshot", {"submissions": [], "pending": [], "questions": []}), ("history", {"history": history})]
        self.controller._run()
        self.assertEqual(self.states[-1], "服务器已保存，待整理")
        self.assertNotIn("已读取最新结果", self.states[-1])

    def test_formal_ack_refresh_shows_verified_stage(self):
        self.controller.submit("saved")
        body = self.controller.local.queued()[0]
        sid = body["client_submission_id"]
        self.controller.local.acknowledge(sid, {"stage": "server_saved", "observation_id": "stable"})
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        history = {"day_start_hour": 7, "date": self.controller.day, "as_of_ms": 1000, "activities": [], "summary": {"human_seconds": None}}
        self.transport.actions = [("snapshot", {"server_now_ms": 1000, "submissions": [{"client_submission_id": sid, "stage": "archived", "observation_id": "stable"}], "pending": [], "questions": []}), ("history", {"history": history})]
        self.controller._run()
        self.assertEqual(self.states[-1], "已归档，飞书已核验")

    def test_organized_ai_does_not_wait_for_unneeded_formal_write(self):
        self.controller.submit("saved")
        sid = self.controller.local.queued()[0]["client_submission_id"]
        self.controller.local.acknowledge(sid, {"stage": "organized", "base_required": True})
        self.assertEqual(self.controller.latest_save_status(), "已整理，等待飞书核验")
        self.controller.local.acknowledge(sid, {"stage": "organized", "base_required": False})
        self.assertEqual(self.controller.latest_save_status(), "已整理")

    def _saved_mirror(self, mirror="queued", stage="server_saved"):
        self.controller.submit("durable original")
        body = self.controller.local.queued()[0]
        sid = body["client_submission_id"]
        response = {"client_submission_id": sid, "stage": stage, "observation_id": "stable", "chat_mirror_state": mirror}
        self.controller.local.acknowledge(sid, response)
        return sid, body, response

    def test_mirror_labels_are_separate_from_formal_stage_and_old_backend_compatible(self):
        sid, body, response = self._saved_mirror()
        expected = {"disabled": "", "not_applicable": "", "queued": "聊天同步中", "sending": "聊天同步中", "retry": "聊天同步中", "sent": "已同步到飞书聊天", "outcome_unknown": "聊天同步待确认，原文已保存", "blocked": "聊天同步待确认，原文已保存"}
        for state, suffix in expected.items():
            self.controller.local.acknowledge(sid, {**response, "chat_mirror_state": state})
            message = self.controller.latest_save_status()
            self.assertTrue(message.startswith("服务器已保存，待整理"))
            self.assertEqual(message, "服务器已保存，待整理" + (" · " + suffix if suffix else ""))
            self.assertNotIn("已归档", message)
        self.controller.local.acknowledge(sid, {"stage": "server_saved", "observation_id": "stable"})
        self.assertEqual(self.controller.latest_save_status(), "服务器已保存，待整理")

    def test_fast_mirror_poll_reads_only_status_and_sent_is_not_archived(self):
        sid, body, response = self._saved_mirror()
        self.controller.visible = True
        self.controller._watch_chat_mirror(sid, response, uploaded=True, now=100)
        self.transport.actions = [("status", {"server_now_ms": 2000, "submissions": [{**response, "chat_mirror_state": "sent", "chat_mirror_message_id": "mirror-only"}]})]
        self.assertFalse(self.controller._poll_chat_mirror(now=104.99))
        self.assertTrue(self.controller._poll_chat_mirror(now=105))
        self.assertEqual([op for op, fields in self.transport.calls], ["status"])
        self.assertEqual(self.controller.local.list_submissions()[0]["stage"], "server_saved")
        self.assertEqual(self.controller.local.list_submissions()[0]["text"], body["text"])
        self.assertIn("已同步到飞书聊天", self.states[-1])
        self.assertNotIn(sid, self.controller.mirror_watch)

    def test_fast_mirror_watch_is_hidden_safe_and_expires_without_renewal(self):
        sid, body, response = self._saved_mirror()
        self.controller._watch_chat_mirror(sid, response, uploaded=True, now=100)
        self.assertFalse(self.controller._poll_chat_mirror(now=105))
        self.assertEqual(self.transport.calls, [])
        self.controller.visible = True
        self.transport.actions = [("status", {"server_now_ms": 2000, "submissions": [response]})]
        self.controller._poll_chat_mirror(now=105)
        self.assertEqual(self.controller.mirror_watch[sid], 130)
        self.controller._watch_chat_mirror(sid, response, now=125)
        self.assertEqual(self.controller.mirror_watch[sid], 130)
        self.assertFalse(self.controller._poll_chat_mirror(now=130))
        self.assertEqual(len(self.transport.calls), 1)

    def test_mirror_query_failure_preserves_saved_original_without_requeue(self):
        sid, body, response = self._saved_mirror()
        self.controller.visible = True
        self.controller._watch_chat_mirror(sid, response, uploaded=True, now=100)
        self.transport.actions = [("status", OSError("offline"))]
        self.assertFalse(self.controller._poll_chat_mirror(now=105))
        saved = self.controller.local.list_submissions()[0]
        self.assertEqual((saved["text"], saved["stage"]), (body["text"], "server_saved"))
        self.assertEqual(self.controller.local.queued(), [])
        self.assertTrue(self.states[-1].startswith("服务器已保存，待整理"))
        self.assertIn("原文已保存", self.states[-1])

    def test_mirror_terminal_states_stop_fast_watch_but_keep_archive_receipt(self):
        sid, body, response = self._saved_mirror(stage="archived")
        for terminal in ("sent", "outcome_unknown", "blocked", "disabled", "not_applicable"):
            self.controller._watch_chat_mirror(sid, response, uploaded=True, now=100)
            self.controller._watch_chat_mirror(sid, {**response, "chat_mirror_state": terminal}, now=105)
            self.assertNotIn(sid, self.controller.mirror_watch)
            self.controller.local.acknowledge(sid, {**response, "chat_mirror_state": terminal})
            self.assertTrue(self.controller.latest_save_status().startswith("已归档，飞书已核验"))

    def test_run_fast_status_does_not_repeat_history_or_invoke_model(self):
        sid, body, response = self._saved_mirror()
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        self.controller._watch_chat_mirror(sid, response, uploaded=True, now=100)
        history = {"day_start_hour": 7, "date": self.controller.day, "as_of_ms": 1000, "activities": [], "summary": {"human_seconds": None}}
        today = datetime.now(LOCAL_TZ).date()
        self.transport.actions = [
            ("snapshot", {"server_now_ms": 1000, "submissions": [response], "pending": [], "questions": []}),
            ("history", {"history": history}),
            ("history", {"date": today.isoformat(), "day_start_hour": 7, "as_of_ms": 1000,
                         "days": [{"day_start_hour": 7, "date": today.isoformat()}]}),
            ("status", {"server_now_ms": 2000, "submissions": [{**response, "chat_mirror_state": "sent"}]}),
        ]
        clock = [100]
        class StepWakeup:
            def set(self): pass
            def clear(self): pass
            def wait(self, timeout): clock[0] += timeout
        self.controller.wakeup = StepWakeup()
        with patch("time_logger.desktop_controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller._run()
        self.assertEqual([op for op, fields in self.transport.calls], ["snapshot", "history", "history", "status"])
        self.assertEqual(len(self.snapshots), 1)

    def _stepping_worker(self, on_wait=None):
        clock = [100.0]
        attempts = []
        self.transport.on_request = lambda op, fields: attempts.append((op, clock[0]))

        class StepWakeup:
            signaled = False
            def set(self): self.signaled = True
            def clear(self): self.signaled = False
            def wait(self, timeout):
                if on_wait:
                    on_wait(clock[0])
                if not self.signaled:
                    clock[0] += timeout
                return self.signaled

        self.controller.wakeup = StepWakeup()
        return clock, attempts

    def test_hidden_offline_queue_recovers_with_bounded_backoff_and_original_payload(self):
        self.controller.submit("one original offline report")
        original = self.controller.local.queued()[0]
        self.transport.actions = [("submit", OSError("offline"))] * 6 + [
            ("submit", {"server_now_ms": 2000, "submission": {"stage": "server_saved", "observation_id": "one"}})]
        clock, attempts = self._stepping_worker()
        with patch("time_logger.desktop_controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller._run()
        times = [at for op, at in attempts]
        self.assertEqual([b - a for a, b in zip(times, times[1:])], [5, 10, 20, 30, 30, 30])
        self.assertTrue(all(body == original for op, body in self.transport.calls))
        self.assertEqual({op for op, body in self.transport.calls}, {"submit"})
        self.assertEqual(self.controller.local.queued(), [])
        self.assertEqual(self.controller.local.list_submissions()[0]["stage"], "server_saved")

    def test_manual_retry_interrupts_backoff_and_duplicate_clicks_merge(self):
        self.controller.submit("retry the same report")
        original = self.controller.local.queued()[0]
        clicks = []
        def on_wait(now):
            if not clicks:
                clicks.extend([self.controller.retry_sync(), self.controller.retry_sync()])
        clock, attempts = self._stepping_worker(on_wait)
        self.transport.actions = [("submit", OSError("offline")),
            ("submit", {"server_now_ms": 2000, "submission": {"stage": "server_saved", "observation_id": "same"}})]
        with patch("time_logger.desktop_controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller._run()
        self.assertEqual(clicks, [True, False])
        self.assertEqual([at for op, at in attempts], [100, 100])
        self.assertEqual([body for op, body in self.transport.calls], [original, original])
        self.assertIsNone(self.controller.thread)

    def test_retry_during_running_request_leaves_one_followup_in_same_worker(self):
        self.controller.submit("one busy report")
        clock, attempts = self._stepping_worker()
        clicks, busy = [], []
        self.controller.sync_busy.connect(busy.append)
        record_attempt = self.transport.on_request
        def during_request(op, fields):
            record_attempt(op, fields)
            if not clicks:
                clicks.extend([self.controller.retry_sync(), self.controller.retry_sync()])
        self.transport.on_request = during_request
        self.transport.actions = [("submit", OSError("expired_session")),
            ("submit", {"server_now_ms": 2000, "submission": {"stage": "server_saved", "observation_id": "same"}})]
        with patch("time_logger.desktop_controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller._run()
        self.assertEqual(clicks, [True, False])
        self.assertEqual([at for op, at in attempts], [100, 100])
        self.assertEqual(busy, [True, False, True, False])
        self.assertEqual(len(self.controller.local.list_submissions()), 1)

    def test_failed_history_does_not_delay_new_upload_or_revoke_saved_receipt(self):
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        queued = []
        def on_wait(now):
            if not queued:
                self.controller.submit("new report while history is offline")
                queued.append(True)
        clock, attempts = self._stepping_worker(on_wait)
        history = {"day_start_hour": 7, "date": self.controller.day, "as_of_ms": 1000, "activities": []}
        self.transport.actions = [("snapshot", OSError("history_offline")),
            ("submit", {"server_now_ms": 2000, "submission": {"stage": "server_saved", "observation_id": "saved"}}),
            ("snapshot", {"submissions": [], "pending": [], "questions": []}), ("history", {"history": history})]
        with patch("time_logger.desktop_controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller._run()
        self.assertEqual(next(at for op, at in attempts if op == "submit"), 100)
        self.assertEqual(self.controller.local.queued(), [])
        self.assertTrue(self.states[-1].startswith("服务器已保存，待整理"))

    def test_upload_ack_survives_following_history_failure_and_busy_resets(self):
        self.controller.submit("saved before history failed")
        self.controller.visible = True
        busy = []
        self.controller.sync_busy.connect(busy.append)
        self.transport.actions = [
            ("submit", {"server_now_ms": 2000, "submission": {"stage": "server_saved", "observation_id": "durable"}}),
            ("snapshot", {"submissions": [], "pending": [], "questions": []}), ("history", OSError("history_offline"))]
        self.controller._run()
        self.assertEqual(self.controller.local.queued(), [])
        self.assertEqual(self.controller.local.list_submissions()[0]["response"]["observation_id"], "durable")
        self.assertTrue(self.states[-1].startswith("服务器已保存，待整理"))
        self.assertIn("历史", self.states[-1])
        self.assertEqual(busy, [True, False])

    def test_manual_retry_with_saved_receipt_refreshes_without_resubmitting(self):
        sid, body, response = self._saved_mirror(mirror="sent")
        self.controller.visible = True
        self.controller.day = "2026-10-26"
        history = {"day_start_hour": 7, "date": self.controller.day, "as_of_ms": 1000, "activities": []}
        self.transport.actions = [("snapshot", {"submissions": [], "pending": [], "questions": []}),
                                  ("history", {"history": history})]
        self.assertTrue(self.controller.retry_sync())
        self.controller._run()
        self.assertEqual([op for op, fields in self.transport.calls], ["snapshot", "history"])
        self.assertEqual(self.controller.local.list_submissions()[0]["client_submission_id"], sid)
        self.assertEqual(self.controller.local.list_submissions()[0]["response"], response)


    def test_raw_receipt_keeps_saved_stage_without_permanent_processing_backlog(self):
        self.controller.submit("raw record")
        queued = self.controller.local.queued()[0]
        self.controller.local.acknowledge(queued["client_submission_id"], {
            "stage": "server_saved", "processing_enabled": False, "base_required": False,
            "observation_id": "raw-one", "mode": "desktop_only"})
        self.controller._emit_pending()
        self.assertEqual(self.pending[-1], [])
        self.assertEqual(self.controller.latest_save_status(), "服务器已保存原文")
        self.assertEqual(self.controller.local.list_submissions()[0]["stage"], "server_saved")
        self.assertEqual(self.controller.local.list_submissions()[0]["client_submission_id"], queued["client_submission_id"])


if __name__ == "__main__":
    unittest.main()
