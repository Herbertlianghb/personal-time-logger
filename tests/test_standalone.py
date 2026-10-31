"""Desktop-only contracts using temporary stores; no network or real identities."""
from __future__ import annotations

import base64
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from time_logger.calculator import LOCAL_TZ
from time_logger.desktop_bridge import BridgeError, DesktopBridge, register_device
from time_logger.desktop_local import LocalState
from time_logger.service import load_config as load_feishu_config
from time_logger.standalone_bridge import StandaloneBridge, load_config, open_store, main
from time_logger.store import Store, now_ms


DAY = "2026-10-01"
STAMP = int(datetime(2026, 10, 1, 12, tzinfo=LOCAL_TZ).timestamp() * 1000)


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-standalone-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.config = {"mode": "desktop_only", "owner_id": "test-owner-01",
                       "database": str(self.root / "raw.sqlite3"), "desktop_enabled": True, "ingest": True}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.config = load_config(self.path)
        self.store = open_store(self.config)
        self.addCleanup(self.store.close)
        self.device = "windows-test-device-01"
        register_device(self.store, self.device)
        self.bridge = StandaloneBridge(self.store, self.config, self.device)

    def request(self, op="submit", **changes):
        value = {"version": 1, "device_id": self.device, "op": op}
        if op == "submit":
            value.update(client_submission_id=str(uuid.uuid4()), reported_at_ms=STAMP, text="测试原文\n尾部空格  ")
        return {**value, **changes}

    def test_config_needs_local_owner_and_rejects_external_features(self):
        for changes in ({"mode": "feishu"}, {"owner_id": ""}, {"owner_id": "../../owner"},
                        {"database": "relative.sqlite3"}, {"desktop_enabled": 1}, {"ingest": "yes"},
                        {"parse": True}, {"base_sync": True}, {"desktop_chat_mirror": True},
                        {"notion_sync": True}, {"app_id": "foreign-app"}, {"app_secret": "test-only-not-a-secret"}):
            with self.subTest(changes=list(changes)):
                self.path.write_text(json.dumps({**self.config, **changes}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(self.path)

    def test_old_feishu_config_still_requires_credentials(self):
        self.path.write_text(json.dumps({"owner_open_id": "test-owner"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "application_credentials_missing"):
            load_feishu_config(self.path)
        with self.assertRaisesRegex(ValueError, "owner_identity_not_verified"):
            load_feishu_config(self._write({}))

    def _write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def test_existing_feishu_database_is_rejected_without_changing_it(self):
        path = self.root / "existing.sqlite3"
        old = Store(path, "test-feishu-app", ["test-feishu-owner"])
        old.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            open_store({**self.config, "database": str(path)})
        self.assertEqual(path.read_bytes(), before)

    def test_reopen_same_owner_and_reject_changed_owner_or_uninitialized_file(self):
        reopened = open_store(self.config)
        reopened.close()
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            open_store({**self.config, "owner_id": "different-owner"})
        empty = self.root / "empty.sqlite3"
        empty.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "not_initialized"):
            open_store({**self.config, "database": str(empty)})
        self.assertEqual(empty.read_bytes(), b"")

    def test_cannot_construct_bridge_for_another_owner(self):
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            StandaloneBridge(self.store, {**self.config, "owner_id": "another-owner"}, self.device)

    def test_raw_submit_is_durable_and_has_no_jobs_or_feishu_calls(self):
        request = self.request()
        with mock.patch("time_logger.feishu.Feishu", side_effect=AssertionError("no network")):
            response = self.bridge.handle(request)
            snapshot = self.bridge.handle(self.request("snapshot", client_submission_ids=[request["client_submission_id"]]))
            self.bridge.handle(self.request("history", date=DAY))
            self.bridge.handle(self.request("heatmap", month="2026-10"))
        saved = response["submission"]
        self.assertEqual(saved["stage"], "server_saved")
        self.assertFalse(saved["processing_enabled"])
        self.assertEqual(saved["text"], request["text"])
        self.assertIsNone(saved["base_verification"])
        self.assertEqual(snapshot["pending"], [])
        self.assertEqual(snapshot["questions"], [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        reopened = open_store(self.config)
        try:
            row = reopened.db.execute("SELECT * FROM desktop_submissions").fetchone()
            self.assertEqual(row["reported_at_ms"], STAMP)
            self.assertEqual(row["raw_text"], request["text"])
        finally:
            reopened.close()

    def test_old_client_offline_queue_survives_lost_ack_and_retry(self):
        path = self.root / "old-client.sqlite3"
        local = LocalState(path)
        original = local.submit("此前已在本机保存的原文")
        local.set_preference("draft", "尚未提交的草稿")
        local.close()
        local = LocalState(path)
        self.addCleanup(local.close)
        queued = local.queued()[0]
        self.assertEqual(queued, original)
        request = self.request(**queued)
        first = self.bridge.handle(request)
        self.assertEqual(local.queued()[0], original)  # response lost
        second = self.bridge.handle(request)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["submission"]["observation_id"], second["submission"]["observation_id"])
        local.acknowledge(original["client_submission_id"], second["submission"])
        self.assertEqual(local.queued(), [])
        saved = local.list_submissions()[0]
        self.assertEqual(saved["client_submission_id"], original["client_submission_id"])
        self.assertEqual(saved["reported_at_ms"], original["reported_at_ms"])
        self.assertEqual(local.preference("draft"), "尚未提交的草稿")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        with self.assertRaisesRegex(BridgeError, "payload_conflict"):
            self.bridge.handle({**request, "text": "不能覆盖原文"})

    def test_devices_remain_registered_revocable_and_bound_to_ssh_identity(self):
        with self.assertRaisesRegex(BridgeError, "device_mismatch"):
            self.bridge.handle(self.request(device_id="windows-other-device"))
        unregistered = StandaloneBridge(self.store, self.config, "windows-unregistered")
        with self.assertRaisesRegex(BridgeError, "device_revoked"):
            unregistered.handle(self.request(device_id="windows-unregistered"))
        for operation in ("register_device", "shell", "prepare_correction"):
            with self.assertRaises(BridgeError):
                self.bridge.handle(self.request(operation))
        register_device(self.store, self.device, False)
        with self.assertRaisesRegex(BridgeError, "device_revoked"):
            self.bridge.handle(self.request())

    def test_disabled_ingest_and_formal_context_cannot_write(self):
        self.config["ingest"] = False
        with self.assertRaisesRegex(BridgeError, "ingestion_disabled"):
            self.bridge.handle(self.request())
        self.config["ingest"] = True
        for changes in ({"activity_id": "another", "expected_revision": 1},
                        {"question_id": "old-question"}, {"record_id": "foreign-record"}):
            with self.assertRaisesRegex(BridgeError, "raw_records_only"):
                self.bridge.handle(self.request(**changes))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0)

    def test_two_independent_owners_cannot_read_each_others_records(self):
        self.bridge.handle(self.request())
        other_config = {**self.config, "owner_id": "another-owner", "database": str(self.root / "other.sqlite3")}
        other_store = open_store(other_config)
        self.addCleanup(other_store.close)
        register_device(other_store, self.device)
        other = StandaloneBridge(other_store, other_config, self.device)
        self.assertEqual(other.handle(self.request("history", date=DAY))["items"], [])
        self.assertEqual(other.handle(self.request("heatmap", month="2026-10"))["summary"]["record_count"], 0)

    def test_history_keeps_raw_time_unknown_duration_and_snapshot_pagination(self):
        for offset in (0, 2000, 4000):
            self.bridge.handle(self.request(reported_at_ms=STAMP + offset))
        first = self.bridge.handle(self.request("history", date=DAY, limit=2))
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first["total_items"], 3)
        self.assertIsNone(first["summary"]["human_seconds"])
        self.assertEqual(first["summary"]["formal_record_count"], 0)
        item = first["items"][0]
        self.assertEqual(item["report_ms"], STAMP)
        self.assertIsNone(item["start_ms"])
        self.assertIsNone(item["duration_seconds"])
        self.assertFalse(item["editable"])
        self.bridge.handle(self.request(reported_at_ms=STAMP - 1000))
        second = self.bridge.handle(self.request("history", date=DAY, cursor=first["next_cursor"], limit=2))
        self.assertEqual(first["data_revision"], second["data_revision"])
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        fresh = self.bridge.handle(self.request("history", date=DAY))
        self.assertEqual(fresh["total_items"], 4)
        self.assertNotEqual(first["data_revision"], fresh["data_revision"])

    def test_invalid_history_cursor_and_bounds_fail(self):
        for changes in ({"date": "bad"}, {"date": DAY, "limit": True}, {"date": DAY, "limit": 101},
                        {"date": DAY, "cursor": "%%%"}, {"date": DAY, "refresh": "yes"}):
            with self.assertRaises(BridgeError):
                self.bridge.handle(self.request("history", **changes))
        token = {"scope": "different-owner", "date": DAY, "offset": 0, "watermark": 0, "as_of_ms": now_ms()}
        cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
        with self.assertRaisesRegex(BridgeError, "cursor_expired"):
            self.bridge.handle(self.request("history", date=DAY, cursor=cursor))

    def test_heatmap_counts_utc8_dates_without_inventing_time_or_category(self):
        midnight = STAMP - 12 * 3600000
        for stamp in (midnight - 1, midnight, STAMP):
            self.bridge.handle(self.request(reported_at_ms=stamp))
        month = self.bridge.handle(self.request("heatmap", month="2026-10"))
        self.assertEqual(month["days"][0]["record_count"], 2)
        self.assertEqual(month["summary"]["record_count"], 2)
        self.assertEqual(month["summary"]["categories"]["unknown"]["record_count"], 2)
        self.assertEqual(month["summary"]["categories"]["工作"]["record_count"], 0)
        self.assertIsNone(month["summary"]["recorded_seconds"])
        self.assertIsNone(month["summary"]["human_seconds"])
        year = self.bridge.handle(self.request("heatmap", month="2026-10", scope="year"))
        self.assertEqual(year["scope"], "year")
        self.assertEqual(year["summary"]["record_count"], 3)

    def test_cli_registers_existing_device_id_without_feishu_config(self):
        other_device = "windows-existing-identity"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            main(["--config", str(self.path), "--device-id", other_device, "--register-device"])
        self.assertEqual(json.loads(output.getvalue()), {"device_id": other_device, "enabled": True})
        bridge = StandaloneBridge(self.store, self.config, other_device)
        response = bridge.handle(self.request(device_id=other_device))
        self.assertEqual(response["submission"]["device_id"], other_device)

    def test_legacy_bridge_still_creates_processing_jobs(self):
        store = Store(self.root / "legacy.sqlite3", "test-app", ["test-owner"])
        self.addCleanup(store.close)
        register_device(store, self.device)
        legacy = DesktopBridge(store, {"owner_open_id": "test-owner", "desktop_enabled": True}, self.device)
        result = legacy.handle(self.request())
        self.assertNotIn("processing_enabled", result["submission"])
        self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE job_type='interpret'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
