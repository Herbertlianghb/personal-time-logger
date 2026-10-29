import tempfile
import unittest
from pathlib import Path

from time_logger.desktop_local import LocalState


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "desktop.sqlite3"
        self.local = LocalState(self.path)

    def tearDown(self):
        self.local.close()
        self.temp.cleanup()

    def test_submit_survives_restart_and_clears_only_submitted_draft(self):
        self.local.set_preference("draft", "hello")
        body = self.local.submit("hello")
        self.assertEqual(self.local.preference("draft"), "")
        self.local.close()
        self.local = LocalState(self.path)
        self.assertEqual(self.local.queued(), [body])
        self.assertEqual(self.local.list_submissions()[0]["stage"], "local_saved")

    def test_failure_retains_same_id_timestamp_and_body(self):
        body = self.local.submit("offline")
        self.local.failed(body["client_submission_id"], "network")
        self.assertEqual(self.local.queued(), [body])
        self.assertEqual(self.local.list_submissions()[0]["error"], "network")

    def test_unknown_remote_stage_cannot_acknowledge_saved(self):
        body = self.local.submit("keep")
        with self.assertRaises(ValueError):
            self.local.acknowledge(body["client_submission_id"], {"stage": "received_in_memory"})
        self.assertEqual(self.local.queued(), [body])

    def test_confirmed_server_stage_stops_upload_without_claiming_archived(self):
        body = self.local.submit("keep")
        self.local.acknowledge(body["client_submission_id"], {"stage": "server_saved", "observation_id": "stable"})
        self.assertEqual(self.local.queued(), [])
        self.assertEqual(self.local.list_submissions()[0]["stage"], "server_saved")

    def test_older_stage_cannot_regress_archived(self):
        body = self.local.submit("keep")
        sid = body["client_submission_id"]
        self.local.acknowledge(sid, {"stage": "archived", "observation_id": "stable"})
        self.local.acknowledge(sid, {"stage": "server_saved", "observation_id": "stable"})
        self.assertEqual(self.local.list_submissions()[0]["stage"], "archived")

    def test_failed_or_stale_cache_does_not_replace_good_history(self):
        good = {"date": "2026-10-26", "as_of_ms": 20, "data_revision": "new", "activities": [{"id": "stable"}]}
        self.local.save_history(good)
        self.local.save_history({"date": "2026-10-26", "as_of_ms": 30, "activities": [], "cache": {"stale": True}})
        self.assertEqual(self.local.cache("2026-10-26"), good)
        self.local.save_history({"date": "2026-10-26", "as_of_ms": 30, "activities": [], "error": "unavailable"})
        self.assertEqual(self.local.cache("2026-10-26"), good)

    def test_late_older_snapshot_does_not_replace_newer_history(self):
        good = {"date": "2026-10-26", "as_of_ms": 20, "data_revision": "new", "activities": [{"id": "stable"}]}
        self.local.save_history(good)
        self.local.save_history({"date": "2026-10-26", "as_of_ms": 10, "data_revision": "old", "activities": []})
        self.assertEqual(self.local.cache("2026-10-26"), good)

    def test_empty_input_rejected_but_question_skip_durable(self):
        with self.assertRaises(ValueError):
            self.local.submit("  ")
        body = self.local.submit("", question_id="stable", disposition="skipped")
        self.assertEqual(self.local.queued(), [body])

    def test_draft_cache_and_queue_remain_separate(self):
        self.local.set_preference("draft", "unsent")
        self.local.save_history({"date": "2026-10-26", "as_of_ms": 10, "activities": []})
        self.assertEqual(self.local.queued(), [])
        self.assertEqual(self.local.preference("draft"), "unsent")


if __name__ == "__main__":
    unittest.main()
