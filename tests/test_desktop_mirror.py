"""Isolated bot mirror delivery/recovery tests. No real messages are sent."""
from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from time_logger.desktop_bridge import DesktopBridge, register_device
from time_logger.desktop_mirror import JOB_TYPE, MAX_HISTORY_PAGES, MirrorError, SAFE_UUID_RETRY_MS, backfill_mirrors, process_mirror
from time_logger.feishu import Feishu, PlatformError, normalize_history, reconcile
from time_logger.service import Service
from time_logger.store import Store, now_ms


class FakePlatform:
    def __init__(self, store):
        self.store = store
        self.posts, self.gets, self.histories = [], [], []
        self.messages, self.tokens = {}, {}
        self.lose_response = self.fail_before_send = self.fail_get = False
        self.history_pages = None
        self.real_timestamp = now_ms()

    def send_desktop_mirror(self, chat_id, text, token):
        # This hook runs where the real HTTP mutation would run. Evidence and
        # the durable outbox must already be visible to another connection.
        assert not self.store.db.in_transaction
        with closing(sqlite3.connect(self.store.path)) as second:
            assert second.execute("SELECT count(*) FROM desktop_submissions").fetchone()[0] > 0
        self.posts.append((chat_id, text, token))
        if self.fail_before_send:
            self.fail_before_send = False
            raise PlatformError("transport_outcome_unknown")
        if token not in self.tokens:
            mid = "om_test_mirror_" + str(len(self.messages) + 1)
            self.tokens[token] = mid
            self.messages[mid] = {"message_id": mid, "chat_id": chat_id, "msg_type": "text", "create_time": str(self.real_timestamp),
                "sender": {"sender_type": "app", "id_type": "app_id", "id": self.store.scope},
                "body": {"content": json.dumps({"text": text}, ensure_ascii=False)}}
        mid = self.tokens[token]
        if self.lose_response:
            self.lose_response = False
            raise PlatformError("transport_outcome_unknown")
        return {"message_id": mid, "sent_at_ms": str(self.real_timestamp)}

    def get_message(self, mid):
        self.gets.append(mid)
        if self.fail_get:
            raise PlatformError("transport_failed")
        return copy.deepcopy(self.messages.get(mid))

    def history_page(self, chat_id, start_ms, end_ms, page_token=None):
        self.histories.append((chat_id, start_ms, end_ms, page_token))
        if self.history_pages is not None:
            return copy.deepcopy(self.history_pages[int(page_token or "0")])
        return {"items": [copy.deepcopy(value) for value in self.messages.values()], "has_more": False}


class DesktopMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-mirror-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3", "test-app", ["test-owner"], ["test-chat"])
        self.addCleanup(lambda: self.store.close())
        self.config = {"database": str(self.store.path), "app_id": "test-app", "app_secret": "test-placeholder",
                       "owner_open_id": "test-owner", "chat_id": "test-chat", "binding_verified": True,
                       "desktop_enabled": True, "desktop_chat_mirror": True, "parse": True}
        self.store.set_setting("bound_chat_id", "test-chat")
        register_device(self.store, "desktop-test-device")
        self.bridge = DesktopBridge(self.store, self.config, "desktop-test-device")
        self.platform = FakePlatform(self.store)

    def request(self, **changes):
        return {"version": 1, "op": "submit", "device_id": "desktop-test-device", "client_submission_id": str(uuid.uuid4()),
                "reported_at_ms": now_ms(), "text": "受控测试：保留原文\n第二行  ", **changes}

    def submit(self, **changes):
        request = self.request(**changes)
        return request, self.bridge.handle(request)["submission"]

    def mirror(self, oid):
        return dict(self.store.db.execute("SELECT * FROM desktop_chat_mirrors WHERE observation_id=?", (oid,)).fetchone())

    def run_job(self, *, force_due=False):
        if force_due:
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET next_at_ms=0,lease_until_ms=0 WHERE job_type=? AND status!='done'", (JOB_TYPE,))
        job = self.store.claim_job([JOB_TYPE], lease_ms=120000)
        self.assertIsNotNone(job)
        process_mirror(self.store, self.config, self.platform, job)
        return job

    def status(self, request):
        return self.bridge.handle({"version": 1, "op": "status", "device_id": request["device_id"],
                                   "client_submission_id": request["client_submission_id"]})["submissions"][0]

    def test_new_original_is_committed_before_send_and_chat_is_separate_stage(self):
        request, source = self.submit()
        self.assertEqual(source["stage"], "server_saved")
        self.assertEqual(source["chat_mirror_state"], "queued")
        self.assertIsNone(source["chat_mirror_message_id"])
        self.assertEqual(self.platform.posts, [])
        job = self.run_job()
        current = self.status(request)
        self.assertEqual(current["stage"], "server_saved")
        self.assertEqual(current["chat_mirror_state"], "sent")
        self.assertEqual(current["chat_mirror_sent_at_ms"], self.platform.real_timestamp)
        self.assertIsNotNone(current["chat_mirror_confirmed_at_ms"])
        self.assertEqual(current["chat_mirror_message_id"], self.platform.gets[0])
        body = self.platform.posts[0][1]
        self.assertTrue(body.startswith("电脑记录 · "))
        self.assertIn("\n" + request["text"] + "\n\n[记录索引 ", body)
        self.assertNotIn(request["device_id"], body)
        self.assertNotIn(source["observation_id"], body)
        self.assertEqual(uuid.UUID(self.platform.posts[0][2]).version, 4)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0], "done")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='interpret'").fetchone()[0], 1)

    def test_duplicate_submit_and_replayed_job_do_not_send_twice(self):
        request, source = self.submit()
        self.bridge.handle(request)
        job = self.run_job()
        process_mirror(self.store, self.config, self.platform, job)
        self.bridge.handle(request)
        self.assertEqual(len(self.platform.posts), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type=?", (JOB_TYPE,)).fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM desktop_chat_mirrors").fetchone()[0], 1)

    def test_raw_and_mirror_job_rollback_together_before_network(self):
        with mock.patch("time_logger.desktop_mirror.enqueue_mirror", side_effect=OSError("test-only-disk-full")):
            with self.assertRaises(OSError):
                self.bridge.handle(self.request())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.platform.posts, [])

    def test_lost_post_response_recovers_by_own_bot_marker_without_second_send(self):
        request, source = self.submit()
        self.platform.lose_response = True
        self.run_job()
        unknown = self.status(request)
        self.assertEqual(unknown["chat_mirror_state"], "outcome_unknown")
        self.assertIsNone(unknown["chat_mirror_message_id"])
        self.run_job(force_due=True)
        self.assertEqual(self.status(request)["chat_mirror_state"], "sent")
        self.assertEqual(len(self.platform.posts), 1)
        self.assertEqual(len(self.platform.messages), 1)
        self.assertEqual(len(self.platform.histories), 1)

    def test_response_lost_before_create_retries_identical_uuid_only_inside_window(self):
        request, source = self.submit()
        self.platform.fail_before_send = True
        self.run_job()
        self.run_job(force_due=True)
        self.assertEqual(len(self.platform.posts), 2)
        self.assertEqual(self.platform.posts[0], self.platform.posts[1])
        self.assertEqual(len(self.platform.messages), 1)
        self.assertEqual(self.status(request)["chat_mirror_state"], "sent")

    def test_expired_unknown_delivery_never_blindly_sends_again(self):
        request, source = self.submit()
        self.platform.fail_before_send = True
        self.run_job()
        with self.store.transaction() as db:
            db.execute("UPDATE desktop_chat_mirrors SET send_started_at_ms=? WHERE observation_id=?", (now_ms() - SAFE_UUID_RETRY_MS - 1000, source["observation_id"]))
        self.run_job(force_due=True)
        self.assertEqual(len(self.platform.posts), 1)
        self.assertEqual(self.status(request)["chat_mirror_state"], "outcome_unknown")
        self.assertEqual(self.status(request)["chat_mirror_error"], "mirror_delivery_unconfirmed")

    def test_post_id_persisted_before_failed_get_then_recovers_exact_id_after_window(self):
        request, source = self.submit()
        self.platform.fail_get = True
        self.run_job()
        row = self.mirror(source["observation_id"])
        self.assertIsNotNone(row["platform_message_id"])
        self.assertIsNotNone(row["accepted_at_ms"])
        self.assertIsNone(row["confirmed_at_ms"])
        self.assertEqual(row["state"], "outcome_unknown")
        with self.store.transaction() as db:
            db.execute("UPDATE desktop_chat_mirrors SET send_started_at_ms=? WHERE observation_id=?", (now_ms() - 2 * 3600000, source["observation_id"]))
        self.platform.fail_get = False
        self.run_job(force_due=True)
        self.assertEqual(self.status(request)["chat_mirror_state"], "sent")
        self.assertEqual(len(self.platform.posts), 1)
        self.assertEqual(len(self.platform.gets), 2)
        self.assertEqual(self.platform.histories, [])

    def test_post_accepted_then_get_auth_error_never_resets_delivery_or_reposts(self):
        for category in ("permission_or_authentication", "http_rejected"):
            with self.subTest(category=category):
                request, source = self.submit()
                prior_posts = len(self.platform.posts)
                with mock.patch.object(self.platform, "get_message", side_effect=PlatformError(category)):
                    self.run_job()
                    row = self.mirror(source["observation_id"])
                    self.assertIsNotNone(row["platform_message_id"])
                    self.assertIsNotNone(row["send_started_at_ms"])
                    self.assertIsNone(row["confirmed_at_ms"])
                    # Even a legacy/incomplete local send-boundary field must
                    # not override the already persisted provider message id.
                    with self.store.transaction() as db:
                        db.execute("UPDATE desktop_chat_mirrors SET send_started_at_ms=NULL WHERE observation_id=?", (source["observation_id"],))
                    self.run_job(force_due=True)
                    self.assertEqual(len(self.platform.posts), prior_posts + 1)
                self.run_job(force_due=True)
                self.assertEqual(self.status(request)["chat_mirror_state"], "sent")
                self.assertEqual(len(self.platform.posts), prior_posts + 1)

    def test_process_restart_retains_id_uuid_and_original(self):
        request, source = self.submit()
        self.platform.lose_response = True
        self.run_job()
        original = self.mirror(source["observation_id"])
        self.store.close()
        self.store = Store(self.config["database"], "test-app", ["test-owner"], ["test-chat"])
        self.bridge = DesktopBridge(self.store, self.config, "desktop-test-device")
        self.platform.store = self.store
        self.run_job(force_due=True)
        current = self.mirror(source["observation_id"])
        self.assertEqual(current["provider_uuid"], original["provider_uuid"])
        self.assertEqual(current["body_hash"], original["body_hash"])
        self.assertEqual(self.status(request)["text"], request["text"])
        self.assertEqual(len(self.platform.posts), 1)

    def test_pause_keeps_pending_without_sending_or_affecting_interpretation(self):
        request, source = self.submit()
        self.config["desktop_chat_mirror"] = False
        self.run_job()
        self.assertEqual(self.platform.posts, [])
        self.assertTrue(self.status(request)["chat_mirror_paused"])
        self.assertIsNotNone(self.store.claim_job(["interpret"]))
        self.config["desktop_chat_mirror"] = True
        self.run_job(force_due=True)
        self.assertEqual(self.status(request)["chat_mirror_state"], "sent")

    def test_known_rejection_retries_without_ambiguous_success_claim(self):
        request, source = self.submit()
        with mock.patch.object(self.platform, "send_desktop_mirror", side_effect=PlatformError("rate_limited")):
            self.run_job()
        row = self.mirror(source["observation_id"])
        self.assertEqual(row["state"], "retry")
        self.assertIsNone(row["send_started_at_ms"])
        self.assertIsNone(row["confirmed_at_ms"])
        self.run_job(force_due=True)
        self.assertEqual(self.status(request)["chat_mirror_state"], "sent")

    def test_unverified_or_changed_binding_does_not_send_to_other_chat(self):
        request, source = self.submit()
        self.config["chat_id"] = "different-chat"
        self.run_job()
        self.assertEqual(self.platform.posts, [])
        self.assertEqual(self.status(request)["chat_mirror_state"], "blocked")
        self.assertEqual(self.status(request)["stage"], "server_saved")

    def test_changed_payload_or_provider_uuid_is_not_sent(self):
        request, source = self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE desktop_chat_mirrors SET body_text='test-only-tamper' WHERE observation_id=?", (source["observation_id"],))
        self.run_job()
        self.assertEqual(self.platform.posts, [])
        self.assertEqual(self.status(request)["chat_mirror_state"], "blocked")

    def test_incomplete_history_never_retries_post(self):
        request, source = self.submit()
        self.platform.fail_before_send = True
        self.run_job()
        self.platform.history_pages = [{"items": [], "has_more": True, "page_token": str(index + 1)} for index in range(MAX_HISTORY_PAGES)]
        self.run_job(force_due=True)
        self.assertEqual(len(self.platform.posts), 1)
        self.assertEqual(self.status(request)["chat_mirror_error"], "mirror_history_page_bound")

    def test_wrong_sender_chat_or_body_cannot_confirm_a_mirror(self):
        request, source = self.submit()
        self.platform.lose_response = True
        self.run_job()
        original = copy.deepcopy(next(iter(self.platform.messages.values())))
        with self.store.transaction() as db:
            db.execute("UPDATE desktop_chat_mirrors SET send_started_at_ms=? WHERE observation_id=?", (now_ms() - 2 * 3600000, source["observation_id"]))
        variants = []
        for sender in ({"sender_type": "user", "id_type": "open_id", "id": "test-owner"},
                       {"sender_type": "app", "id_type": "app_id", "id": "other-app"}):
            item = copy.deepcopy(original)
            item["sender"] = sender
            variants.append(item)
        wrong_chat = copy.deepcopy(original)
        wrong_chat["chat_id"] = "other-chat"
        variants.append(wrong_chat)
        changed = copy.deepcopy(original)
        changed["body"]["content"] = json.dumps({"text": self.mirror(source["observation_id"])["marker"] + "wrong body"})
        variants.append(changed)
        self.platform.history_pages = [{"items": variants, "has_more": False}]
        self.run_job(force_due=True)
        self.assertEqual(self.status(request)["chat_mirror_state"], "outcome_unknown")
        self.assertIsNone(self.status(request)["chat_mirror_confirmed_at_ms"])
        self.assertEqual(len(self.platform.posts), 1)

    def test_direct_get_mismatch_never_claims_sent_or_reposts(self):
        request, source = self.submit()
        self.platform.fail_get = True
        self.run_job()
        self.platform.fail_get = False
        next(iter(self.platform.messages.values()))["chat_id"] = "wrong-chat"
        self.run_job(force_due=True)
        self.assertEqual(self.status(request)["chat_mirror_state"], "outcome_unknown")
        self.assertIsNone(self.status(request)["chat_mirror_confirmed_at_ms"])
        self.assertEqual(len(self.platform.posts), 1)

    def test_bot_mirror_is_ignored_by_sdk_and_history_but_real_user_copy_is_evidence(self):
        request, source = self.submit()
        self.run_job()
        item = next(iter(self.platform.messages.values()))
        self.assertIsNone(normalize_history(item, "test-app", "test-chat", {"test-owner"}))
        service = object.__new__(Service)
        service.store, service.config = self.store, self.config
        event = SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(chat_type="p2p"), sender=SimpleNamespace(sender_type="app")))
        service.receive(event)
        result = reconcile(self.store, self.platform, "test-chat", now_ms() - 60000)
        self.assertEqual(result["new_observations"], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        user_copy = copy.deepcopy(item)
        user_copy["message_id"] = "om_user_copy_test"
        user_copy["sender"] = {"sender_type": "user", "id": "test-owner"}
        normalized = normalize_history(user_copy, "test-app", "test-chat", {"test-owner"})
        self.assertIsNotNone(normalized)
        self.store.ingest(normalized)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)

    def test_mirror_recall_is_not_a_user_recall_and_unknown_recall_waits_for_sender(self):
        request, source = self.submit()
        self.run_job()
        service = object.__new__(Service)
        service.store, service.config = self.store, self.config
        mid = self.status(request)["chat_mirror_message_id"]
        event = SimpleNamespace(event=SimpleNamespace(chat_id="test-chat", message_id=mid, recall_time=str(now_ms())), header=SimpleNamespace(event_id="recall-test"))
        service.recalled(event)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.status(request)["chat_mirror_error"], "chat_mirror_recalled")
        event.event.message_id = "om_unknown_bot_mirror"
        service.recalled(event)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='reconcile'").fetchone()[0], 1)

    def test_backfill_only_selected_device_two_real_originals_skips_trusted_test(self):
        self.config["desktop_chat_mirror"] = False
        originals = [self.submit()[1] for _ in range(3)]
        source = self.store.evidence([originals[2]["observation_id"]])[0]
        with self.store.transaction() as db:
            db.execute("INSERT INTO acceptance_tests VALUES(?,?)", (self.store.scope, source["message_id"]))
        before_jobs = self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        count = backfill_mirrors(self.store, self.config, "desktop-test-device")
        self.assertEqual(count["eligible_count"], 2)
        self.assertFalse(count["applied"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], before_jobs)
        self.config["desktop_chat_mirror"] = True
        self.assertEqual(backfill_mirrors(self.store, self.config, "desktop-test-device", apply=True)["queued_count"], 2)
        self.assertEqual(backfill_mirrors(self.store, self.config, "desktop-test-device", apply=True)["queued_count"], 0)
        self.run_job()
        self.run_job()
        self.assertEqual(len(self.platform.posts), 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 3)

    def test_test_tag_added_before_delivery_stops_even_first_network_request(self):
        request, source = self.submit()
        evidence = self.store.evidence([source["observation_id"]])[0]
        with self.store.transaction() as db:
            db.execute("INSERT INTO acceptance_tests VALUES(?,?)", (self.store.scope, evidence["message_id"]))
        self.run_job()
        self.assertEqual(self.platform.posts, [])
        self.assertEqual(self.platform.gets, [])
        self.assertEqual(self.platform.histories, [])
        self.assertEqual(self.status(request)["chat_mirror_state"], "not_applicable")

    def test_backfill_more_than_ten_never_queues(self):
        self.config["desktop_chat_mirror"] = False
        for _ in range(11):
            self.submit()
        self.config["desktop_chat_mirror"] = True
        with self.assertRaisesRegex(MirrorError, "explicit_bound"):
            backfill_mirrors(self.store, self.config, "desktop-test-device", apply=True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type=?", (JOB_TYPE,)).fetchone()[0], 0)

    def test_api_send_uses_stable_uuid_and_get_parses_platform_milliseconds(self):
        platform = Feishu("test-app", "test-placeholder")
        token = str(uuid.uuid4())
        item = {"message_id": "om_test", "create_time": "1793110495134"}
        with mock.patch.object(platform, "request", side_effect=[{"data": item}, {"data": {"items": [item]}}]) as request:
            result = platform.send_desktop_mirror("test-chat", "原文", token)
            self.assertEqual(result["sent_at_ms"], "1793110495134")
            self.assertEqual(request.call_args_list[0].args[2]["uuid"], token)
            self.assertEqual(platform.get_message("om_test"), item)
            self.assertEqual(request.call_args_list[1].args[:2], ("GET", "/im/v1/messages/om_test"))


if __name__ == "__main__":
    unittest.main()
