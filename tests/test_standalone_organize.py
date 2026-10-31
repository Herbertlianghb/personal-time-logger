"""Manual organization uses only synthetic, owner-bound desktop evidence."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from time_logger.desktop_bridge import BridgeError, register_device
from time_logger.standalone_bridge import StandaloneBridge, load_config, open_store
from tests.test_batch import body, unknown_block
from time_logger.calculator import LOCAL_TZ

STAMP = int(datetime(2026, 10, 31, 12, tzinfo=LOCAL_TZ).timestamp() * 1000)


class ManualOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-only-manual-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"mode": "desktop_only", "owner_id": "test-manual-owner",
                       "database": str(self.root / "raw.sqlite3"), "desktop_enabled": True,
                       "ingest": True, "manual_organization": True}
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        self.config = load_config(path)
        self.store = open_store(self.config)
        self.addCleanup(self.store.close)
        self.device = "windows-test-manual"
        register_device(self.store, self.device)
        self.bridge = StandaloneBridge(self.store, self.config, self.device)

    def rpc(self, op, **fields):
        return self.bridge.handle({"version": 1, "device_id": self.device, "op": op, **fields})

    def submit(self, text="现在开始学习", stamp=STAMP):
        return self.rpc("submit", text=text, reported_at_ms=stamp, client_submission_id=str(uuid.uuid4()))

    def candidate(self, batch):
        block = unknown_block(batch["evidence"][0])
        block.update(name="学习", category="学习")
        return {"batch_id": batch["batch_id"], "proposal": body([block]),
                "metadata": {"model": "test-only-agent", "prompt_version": "manual-1",
                             "batch_input_hash": batch["input_hash"]}}

    def test_manual_mode_is_opt_in_and_does_not_enqueue_on_submit(self):
        self.submit()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.config["manual_organization"] = False
        with self.assertRaisesRegex(BridgeError, "manual_organization_disabled"):
            self.rpc("agent_export", date="2026-10-31")

    def test_export_is_date_scoped_replayable_and_contains_agent_contract(self):
        saved = self.submit()
        self.submit("另一天的原文", STAMP - 86400000)
        exported = self.rpc("agent_export", date="2026-10-31")
        batch = exported["bundle"]
        self.assertEqual(batch["new_evidence_ids"], [saved["submission"]["observation_id"]])
        self.assertEqual(self.rpc("agent_export", date="2026-10-31")["bundle"]["batch_id"], batch["batch_id"])
        self.assertIn("blocks", exported["proposal_schema"]["properties"])
        self.assertIn("evidence", exported["instructions"])
        self.assertEqual(set(batch["categories"]), {"工作", "生活", "娱乐", "学习", "休息", "运动"})

    def test_import_preserves_originals_updates_timeline_and_never_queues_base(self):
        self.submit()
        original = [tuple(row) for row in self.store.db.execute("SELECT * FROM desktop_submissions")]
        batch = self.rpc("agent_export", date="2026-10-31")["bundle"]
        candidate = self.candidate(batch)
        result = self.rpc("agent_import", **candidate)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["activities_changed"], 1)
        self.assertTrue(self.rpc("agent_import", **candidate)["replayed"])
        self.assertEqual(original, [tuple(row) for row in self.store.db.execute("SELECT * FROM desktop_submissions")])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='base_sync'").fetchone()[0], 0)
        timeline = self.rpc("history", date="2026-10-31")
        self.assertEqual(timeline["mode"], "desktop_organized")
        self.assertEqual(len(timeline["items"]), 1)
        self.assertEqual(timeline["items"][0]["category"], "学习")
        self.assertIsNone(timeline["items"][0]["duration_seconds"])
        self.assertEqual(self.rpc("agent_export", date="2026-10-31")["bundle"]["status"], "idle")

    def test_invalid_category_and_foreign_evidence_never_change_activities(self):
        self.submit()
        batch = self.rpc("agent_export", date="2026-10-31")["bundle"]
        candidate = self.candidate(batch)
        candidate["proposal"]["blocks"][0]["category"] = "自造类别"
        with self.assertRaises(ValueError):
            self.rpc("agent_import", **candidate)
        candidate = self.candidate(batch)
        candidate["proposal"]["blocks"][0]["evidence_refs"] = ["other-owner-evidence"]
        with self.assertRaises(ValueError):
            self.rpc("agent_import", **candidate)
        self.assertEqual(self.store.current_activities(), [])

    def test_dates_export_independently_and_unknown_batches_are_rejected(self):
        self.submit()
        self.submit("此前的活动", STAMP - 86400000)
        first = self.rpc("agent_export", date="2026-10-31")["bundle"]
        other = self.rpc("agent_export", date="2026-10-30")["bundle"]
        self.assertNotEqual(first["batch_id"], other["batch_id"])
        self.assertTrue(set(first["new_evidence_ids"]).isdisjoint(other["new_evidence_ids"]))
        candidate = self.candidate(first)
        candidate["batch_id"] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.rpc("agent_import", **candidate)

    def test_raw_evidence_remains_visible_and_empty_time_is_not_fabricated(self):
        self.submit()
        self.assertEqual(self.rpc("history", date="2026-10-31")["summary"]["raw_record_count"], 1)
        self.assertIsNone(self.rpc("history", date="2026-10-31")["summary"]["recorded_seconds"])
        self.assertFalse(self.rpc("health")["health"]["parse_enabled"])
        self.assertTrue(self.rpc("health")["health"]["capabilities"]["manual_organization"])

    def test_recorded_windows_appear_in_timeline_and_heatmap_without_claiming_human_effort(self):
        self.submit("10:00开始学习，11:00结束")
        batch = self.rpc("agent_export", date="2026-10-31")["bundle"]
        candidate = self.candidate(batch)
        block = candidate["proposal"]["blocks"][0]
        source = batch["evidence"][0]
        for key, hour, quote in (("start", 10, "10:00开始学习"), ("end", 11, "11:00结束")):
            block[key] = {"value_ms": STAMP + (hour - 12) * 3600000, "kind": "explicit_user",
                          "precision": "minute", "source_id": source["id"], "quote": quote,
                          "date_resolution": "contextual"}
        block["status"] = "ended"
        self.rpc("agent_import", **candidate)
        timeline = self.rpc("history", date="2026-10-31")
        self.assertEqual(timeline["summary"]["recorded_seconds"], 3600)
        self.assertIsNone(timeline["summary"]["human_seconds"])
        month = self.rpc("heatmap", month="2026-10")
        day = next(item for item in month["days"] if item["date"] == "2026-10-31")
        self.assertEqual(day["recorded_seconds"], 3600)
        self.assertEqual(day["categories"]["学习"]["recorded_seconds"], 3600)
        self.assertIsNone(day["human_seconds"])

    def test_estimated_boundaries_are_not_actual_nodes_or_confirmed_windows(self):
        self.submit("大约10:00开始学习，约11:00结束")
        batch = self.rpc("agent_export", date="2026-10-31")["bundle"]
        candidate = self.candidate(batch)
        block = candidate["proposal"]["blocks"][0]
        for key, hour in (("start", 10), ("end", 11)):
            block[key] = {"value_ms": STAMP + (hour - 12) * 3600000, "kind": "estimate",
                          "precision": "approximate", "source_id": batch["evidence"][0]["id"],
                          "quote": batch["evidence"][0]["content"], "date_resolution": "contextual"}
        self.rpc("agent_import", **candidate)
        timeline = self.rpc("history", date="2026-10-31")
        item = timeline["items"][0]
        self.assertIsNone(item["start_ms"])
        self.assertIsNone(item["end_ms"])
        self.assertEqual(item["recorded_intervals"], [])
        self.assertIsNone(timeline["summary"]["recorded_seconds"])
        self.assertEqual(timeline["summary"]["estimated_window_seconds"], 3600)
        self.assertEqual(self.rpc("heatmap", month="2026-10")["summary"]["estimated_seconds"], 3600)


    def test_cross_day_estimate_remains_in_the_following_day_history(self):
        self.submit("23:00开始学习，大约次日01:00结束", STAMP + 11 * 3600000)
        batch = self.rpc("agent_export", date="2026-10-31")["bundle"]
        candidate = self.candidate(batch)
        block = candidate["proposal"]["blocks"][0]
        source = batch["evidence"][0]
        block["start"] = {"value_ms": STAMP + 11 * 3600000, "kind": "explicit_user", "precision": "minute",
                          "source_id": source["id"], "quote": "23:00开始学习", "date_resolution": "contextual"}
        block["end"] = {"value_ms": STAMP + 13 * 3600000, "kind": "estimate", "precision": "approximate",
                        "source_id": source["id"], "quote": source["content"], "date_resolution": "contextual"}
        self.rpc("agent_import", **candidate)
        history = self.rpc("history", date="2026-11-01")
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["summary"]["estimated_window_seconds"], 3600)
        self.assertIsNone(history["items"][0]["end_ms"])


class AgentFileTests(unittest.TestCase):
    def test_desktop_entry_handles_exchange_before_loading_gui(self):
        from time_logger.desktop_app import main
        with mock.patch("time_logger.standalone_agent.run") as exchange:
            self.assertEqual(main(["--config", "test-device.json", "--agent-export", "test-batch.json", "--date", "2026-10-31"]), 0)
            self.assertEqual(exchange.call_args.kwargs["day"], "2026-10-31")
            self.assertEqual(exchange.call_args.kwargs["export_path"], "test-batch.json")
        with mock.patch("time_logger.standalone_agent.run", side_effect=ValueError("synthetic-failure")):
            self.assertEqual(main(["--config", "test-device.json", "--agent-export", "test-batch.json"]), 1)

    def test_files_are_new_bounded_and_can_use_existing_device_transport(self):
        from time_logger.standalone_agent import run
        with tempfile.TemporaryDirectory(prefix="test-only-agent-file-") as folder:
            root = Path(folder)
            config = root / "device.json"
            config.write_text("{}", encoding="utf-8")
            output = root / "batch.json"
            with mock.patch("time_logger.standalone_agent.DeviceTransport") as transport:
                transport.return_value.request.return_value = {"ok": True, "bundle": {"status": "idle"}}
                run(config, export_path=output, day="2026-10-31")
                self.assertTrue(output.is_file())
                transport.return_value.request.assert_called_once_with("agent_export", date="2026-10-31")
                transport.return_value.close.assert_called_once()
                with self.assertRaises(FileExistsError):
                    run(config, export_path=output, day="2026-10-31")
            result = root / "result.json"
            candidate = root / "proposal.json"
            candidate.write_text(json.dumps({"batch_id": str(uuid.uuid4()), "proposal": body(), "metadata": {}}), encoding="utf-8")
            with mock.patch("time_logger.standalone_agent.DeviceTransport") as transport:
                transport.return_value.request.return_value = {"accepted": True}
                run(config, import_path=candidate, result_path=result)
                self.assertTrue(json.loads(result.read_text(encoding="utf-8"))["accepted"])
            candidate.write_text("x" * 65537, encoding="utf-8")
            with self.assertRaises(ValueError):
                run(config, import_path=candidate, result_path=root / "too-large.json")


if __name__ == "__main__":
    unittest.main()
