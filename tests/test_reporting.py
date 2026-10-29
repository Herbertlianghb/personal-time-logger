import unittest
import test_core as fixtures
from test_core import block, proposal, timestamp
from time_logger.reporting import report, mark_reported


class ReportingTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp

    def source(self, text="2026年10月27日23:30到2026年10月28日00:30一直工作。"):
        self.store.ingest({"app_scope": "test-only-app", "sender_key": "test-only-owner", "chat_id": "test-only-chat",
                           "message_id": "reporting-fixture", "content": text,
                           "created_at_ms": timestamp("2026-10-28T01:00:00+08:00")})
        return self.store.evidence()[0]

    def test_report_clips_cross_midnight_effort_and_invalidates_both_days(self):
        source = self.source()
        candidate = block(source, timestamp("2026-10-27T23:30:00+08:00"), timestamp("2026-10-28T00:30:00+08:00"), effort=True)
        pid = self.store.save_proposal(proposal([candidate]), [source], "test", "test")
        self.store.accept_proposal(pid)
        result = report(self.store, "2026-10-28")
        self.assertEqual(result["local_accepted_projection"]["human_effort_seconds"], 1800)
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT day FROM report_dirty")}, {"2026-10-27", "2026-10-28"})
        self.assertEqual(result["completeness"], "partial")
        self.assertIn("requires fresh", result["formal_base_status"])

    def test_report_acknowledgement_requires_current_projection_revision(self):
        source = self.source("刚刚结束，实际起止不知道。")
        pid = self.store.save_proposal(proposal([block(source)]), [source], "test", "test")
        self.store.accept_proposal(pid)
        result = report(self.store, "2026-10-28")
        self.assertEqual(result["metrics"]["user_manual_minutes"], None)
        with self.assertRaises(ValueError):
            mark_reported(self.store, "2026-10-28", "wrong-version")
        mark_reported(self.store, "2026-10-28", result["data_revision"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM report_dirty").fetchone()[0], 0)
