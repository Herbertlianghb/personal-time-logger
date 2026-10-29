"""Local control/replay tests only; no real credentials or remote writes."""
from __future__ import annotations

import copy
from datetime import datetime
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock
import urllib.error
import urllib.parse

from time_logger.calculator import interval_union, seconds, split_days, summarize
from time_logger.feishu import Feishu, PlatformError, normalize_history, reconcile
from time_logger.proposal import InvalidProposal, validate_proposal
from time_logger.service import Service
from time_logger.store import Store, digest


CORPUS = json.loads((Path(__file__).parent / "controlled-corpus.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case["items"] for case in CORPUS["cases"]}
BASE = int(datetime.fromisoformat("2026-10-27T09:00:00+08:00").timestamp() * 1000)


def timestamp(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def messages(name):
    return [copy.deepcopy(item["message"]) for item in CASES[name]]


def unknown():
    return {"value_ms": None, "kind": "unknown", "precision": "unknown", "source_id": None,
            "quote": "", "date_resolution": "unknown"}


def claim(source, value, kind="explicit_user"):
    return {"value_ms": value, "kind": kind, "precision": "minute" if kind == "explicit_user" else "millisecond",
            "source_id": source["id"], "quote": source["content"], "date_resolution": "contextual"}


def block(source, start=None, end=None, actor="human", effort=False):
    result = {"activity_id": None, "supersedes_revision": None, "name": "测试活动", "actor": actor,
              "role": "primary", "parent_activity_id": None, "category": None, "project": None,
              "status": "ended" if end is not None else "unknown", "evidence_refs": [source["id"]],
              "start": claim(source, start) if start is not None else unknown(),
              "end": claim(source, end) if end is not None else unknown(), "human_intervals": [],
              "reported_duration_seconds": None, "result": "", "uncertainty": "", "subtasks": []}
    if effort:
        result["human_intervals"] = [{"start_ms": start, "end_ms": end, "source_id": source["id"], "quote": source["content"]}]
    return result


def proposal(blocks=(), questions=()):
    return {"version": 1, "blocks": list(blocks), "questions": list(questions)}


def history_item(message):
    return {"message_id": message["message_id"], "sender": {"id": message["sender_key"], "sender_type": message["sender_type"]},
            "msg_type": message["message_type"], "create_time": str(message["created_at_ms"]),
            "body": {"content": message["content"]}}


class PagedHistory:
    def __init__(self, inputs, fail_page=None):
        self.items = [history_item(item) for item in inputs]
        self.fail_page = fail_page
        self.calls = []

    def history_page(self, chat_id, start_ms, end_ms, page_token=None):
        page = 0 if page_token is None else int(page_token)
        self.calls.append((chat_id, start_ms, end_ms, page_token))
        if page == self.fail_page:
            raise PlatformError("injected_page_failure")
        items = self.items[page * 50:(page + 1) * 50]
        more = (page + 1) * 50 < len(self.items)
        return {"items": items, "has_more": more, "page_token": str(page + 1) if more else None}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="test-only-time-logger-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.store = Store(self.path / "test.sqlite3", "test-only-app", ["test-only-owner"], ["test-only-chat"])
        self.addCleanup(lambda: self.store.close())

    def count(self, table):
        return self.store.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def ingest_case(self, name):
        results = [self.store.ingest(message) for message in messages(name)]
        return self.store.evidence(), results

    def accept(self, body, evidence=None):
        pid = self.store.save_proposal(body, evidence or self.store.evidence(), "test-only-model", "test-only-prompt")
        return pid, self.store.accept_proposal(pid)

    def service(self):
        service = Service({"database": str(self.store.path), "app_id": "test-only-app", "app_secret": "TEST_FIXTURE_NOT_A_SECRET",
                           "owner_open_id": "test-only-owner", "chat_id": "test-only-chat"})
        self.addCleanup(service.store.close)
        service.platform = mock.Mock()
        return service

    @staticmethod
    def event(message):
        return NS(header=NS(event_id=message["event_id"]), event=NS(
            message=NS(message_id=message["message_id"], chat_id=message["chat_id"], chat_type="p2p",
                       message_type=message["message_type"], create_time=str(message["created_at_ms"]), content=message["content"]),
            sender=NS(sender_type=message["sender_type"], sender_id=NS(open_id=message["sender_key"]))))

    def test_fixture_has_123_traceable_nonproduction_inputs(self):
        samples = [item for case in CORPUS["cases"] for item in case["items"]]
        self.assertGreaterEqual(len(samples), 120)
        self.assertEqual(len(samples), CORPUS["sample_count"])
        self.assertEqual(len(samples), len({sample["sample_id"] for sample in samples}))
        self.assertFalse(CORPUS["contains_real_user_data"])
        self.assertTrue(all(sample["test_fixture"] and sample["message"]["message_id"].startswith("test-") for sample in samples))

    def test_message_id_deduplicates_different_event_ids_and_receipts(self):
        evidence, results = self.ingest_case("dedup")
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='receipt'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='interpret'").fetchone()[0], 1)

    def test_first_platform_time_survives_delayed_duplicate(self):
        self.ingest_case("immutable")
        row = self.store.db.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(row["message_created_at_ms"], BASE)
        self.assertEqual(row["first_content"], "开始阅读。")

    def test_identical_text_from_distinct_messages_is_not_deduplicated(self):
        self.ingest_case("distinct")
        self.assertEqual(self.count("messages"), 3)
        self.assertEqual(self.count("observations"), 3)

    def test_recall_tombstone_arrives_before_original(self):
        self.ingest_case("recall_first")
        current = self.store.evidence()[0]
        self.assertEqual(current["kind"], "recall")
        self.assertIsNone(current["content"])
        self.assertEqual(current["created_at_ms"], BASE)
        self.assertEqual(self.count("observations"), 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='receipt'").fetchone()[0], 0)

    def test_old_retry_cannot_replace_observed_newer_edit(self):
        self.ingest_case("edit")
        self.assertEqual(self.store.evidence()[0]["content"], "修订稿。")
        self.assertEqual(self.store.evidence()[0]["updated_at_ms"], BASE + 20000)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("observations"), 3)

    def test_incomparable_content_is_conflict_and_keeps_current(self):
        evidence, results = self.ingest_case("conflict")
        self.assertTrue(results[-1]["conflict"])
        self.assertEqual(evidence[0]["content"], "版本甲。")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='reconcile'").fetchone()[0], 1)

    def test_permission_filters_do_not_store_third_party_body(self):
        for message in messages("permissions"):
            with self.subTest(message_id=message["message_id"]):
                with self.assertRaises(PermissionError):
                    self.store.ingest(message)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.count("observations"), 0)
        self.assertEqual(self.count("jobs"), 0)

    def test_commit_failure_rolls_back_message_and_receipt_job(self):
        good, failed = messages("commit")
        self.store.ingest(good)
        before = self.count("jobs")
        self.store.db.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT" else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.ingest(failed)
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.count("jobs"), before)
        self.assertFalse(self.store.db.in_transaction)

    def test_receiver_never_sends_saved_receipt_before_commit(self):
        service = self.service()
        good, failed = messages("receipt")
        service.receive(self.event(good))
        service.platform.receipt.assert_not_called()
        service.store.db.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT" else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                service.receive(self.event(failed))
        finally:
            service.store.db.set_authorizer(None)
        service.platform.receipt.assert_not_called()
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='receipt'").fetchone()[0], 1)

    def test_restart_preserves_raw_evidence_and_pending_jobs(self):
        self.ingest_case("restart")
        before = [(row["message_id"], row["content_hash"]) for row in self.store.evidence()]
        jobs = self.count("jobs")
        self.store.close()
        self.store = Store(self.path / "test.sqlite3", "test-only-app", ["test-only-owner"], ["test-only-chat"])
        self.assertEqual([(row["message_id"], row["content_hash"]) for row in self.store.evidence()], before)
        self.assertEqual(self.count("jobs"), jobs)

    def test_expired_job_lease_is_reclaimed_with_same_client_token(self):
        self.ingest_case("lease")
        job = self.store.claim_job(["receipt"], lease_ms=60000)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET lease_until_ms=0 WHERE job_id=?", (job["job_id"],))
        reclaimed = self.store.claim_job(["receipt"])
        self.assertEqual(reclaimed["job_id"], job["job_id"])
        self.assertEqual(reclaimed["client_token"], job["client_token"])
        self.assertEqual(reclaimed["attempts"], 2)

    def test_history_50_item_pages_same_second_boundaries_no_omissions(self):
        platform = PagedHistory(messages("history"))
        result = reconcile(self.store, platform, "test-only-chat", BASE, fixed_end_ms=BASE + 1000)
        self.assertEqual(self.count("messages"), 53)
        self.assertEqual(result["new_observations"], 53)
        self.assertEqual(len(platform.calls), 2)
        self.assertEqual(platform.calls[1][3], "1")
        self.assertEqual({row["created_at_ms"] for row in self.store.evidence()}, set(range(BASE + 100, BASE + 153)))
        receipts = self.store.db.execute("SELECT payload FROM jobs WHERE job_type='receipt'").fetchall()
        self.assertEqual(len(receipts), 1)
        self.assertIn("已补回53条消息证据", json.loads(receipts[0][0])["text"])
        reconcile(self.store, platform, "test-only-chat", BASE, fixed_end_ms=BASE + 1000)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE job_type='receipt'").fetchone()[0], 1)

    def test_failed_second_history_page_does_not_advance_watermark(self):
        previous = BASE - 86400000
        with self.store.transaction() as db:
            db.execute("INSERT INTO scans VALUES(?,?,NULL,NULL,'scan_success_completeness_limited',NULL)", ("test-only-chat", previous))
        with self.assertRaises(PlatformError):
            reconcile(self.store, PagedHistory(messages("history"), fail_page=1), "test-only-chat", BASE, fixed_end_ms=BASE + 1000)
        row = self.store.db.execute("SELECT * FROM scans").fetchone()
        self.assertEqual(row["completed_through_ms"], previous)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.count("messages"), 50)
        result = reconcile(self.store, PagedHistory(messages("history")), "test-only-chat", BASE, fixed_end_ms=BASE + 1000)
        self.assertEqual(self.count("messages"), 53)
        self.assertEqual(result["completed_through_ms"], BASE + 1000)

    def test_history_restart_discards_expired_cursor_keeps_fixed_range(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO scans VALUES(?,?,?,?,'scanning',NULL)", ("test-only-chat", BASE - 86400000, BASE + 1000, "expired-cursor"))
        platform = PagedHistory(messages("history"))
        reconcile(self.store, platform, "test-only-chat", BASE, fixed_end_ms=BASE + 999999)
        self.assertIsNone(platform.calls[0][3])
        self.assertEqual(platform.calls[0][2], BASE + 1000)
        self.assertLessEqual(platform.calls[0][1], BASE - 86400000)
        self.assertEqual(self.count("messages"), 53)

    def test_history_rest_contract_outward_rounding_and_page_size(self):
        platform = Feishu("test-app", "TEST_FIXTURE_NOT_A_SECRET")
        platform.request = mock.Mock(return_value={"data": {"items": [], "has_more": False}})
        platform.history_page("test-only-chat", BASE + 101, BASE + 999, "next-test-page")
        path = platform.request.call_args.args[1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        self.assertEqual(query["start_time"], [str(BASE // 1000)])
        self.assertEqual(query["end_time"], [str(BASE // 1000 + 1)])
        self.assertEqual(query["page_size"], ["50"])
        self.assertEqual(query["page_token"], ["next-test-page"])

    def test_cross_day_slices_keep_one_logical_activity(self):
        evidence, _ = self.ingest_case("crossday")
        intervals = [(timestamp("2026-10-26T23:30:00+08:00"), timestamp("2026-10-27T00:30:00+08:00"))]
        item = block(evidence[0], *intervals[0], effort=True)
        pid, changed = self.accept(proposal([item]))
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.count("activities"), 1)
        self.assertEqual(summarize(self.store.current_activities())["human_by_day"], {"2026-10-26": 1800, "2026-10-27": 1800})
        self.assertEqual(sum(split_days(intervals).values()), seconds(intervals))

    def test_parallel_human_intervals_use_union(self):
        evidence, _ = self.ingest_case("overlap")
        first = block(evidence[0], BASE + 3600000, BASE + 7200000, effort=True)
        second = block(evidence[1], BASE + 5400000, BASE + 9000000, effort=True)
        validate_proposal(proposal([first, second]), evidence)
        summary = summarize([first, second])
        self.assertEqual(summary["human_effort_seconds"], 5400)
        self.assertEqual(interval_union([[0, 2], [1, 3], [3, 4]]), [[0, 4]])

    def test_ai_background_does_not_increase_human_total(self):
        evidence, _ = self.ingest_case("ai")
        human = block(evidence[0], BASE + 3600000, BASE + 7200000, effort=True)
        ai = block(evidence[1], BASE + 3600000, BASE + 10800000, actor="ai")
        validate_proposal(proposal([human, ai]), evidence)
        result = summarize([human, ai])
        self.assertEqual(result["human_effort_seconds"], 3600)
        self.assertEqual(result["ai_background_union_seconds"], 7200)
        ai["human_intervals"] = copy.deepcopy(human["human_intervals"])
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([human, ai]), evidence)

    def test_parent_and_detail_cannot_double_count_effort(self):
        evidence, _ = self.ingest_case("detail")
        parent = block(evidence[0], BASE + 3600000, BASE + 7200000, effort=True)
        detail = copy.deepcopy(parent)
        detail["role"] = "detail"
        self.assertEqual(summarize([parent, detail])["human_effort_seconds"], 3600)
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([parent, detail]), evidence)

    def test_revisions_have_one_current_version_and_reject_stale(self):
        evidence, _ = self.ingest_case("revision")
        original = block(evidence[0], BASE + 3600000, BASE + 7200000, effort=True)
        first_pid, changed = self.accept(proposal([original]), evidence)
        aid = changed[0]
        revision = copy.deepcopy(original)
        revision.update(activity_id=aid, supersedes_revision=1, evidence_refs=[evidence[0]["id"], evidence[1]["id"]])
        revision["end"] = claim(evidence[1], BASE + 9000000)
        self.accept(proposal([revision]), evidence)
        self.assertEqual(self.store.current_activities()[0]["revision"], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM activities WHERE current_valid=1").fetchone()[0], 1)
        self.assertEqual(self.count("activities"), 2)
        self.assertEqual(self.store.accept_proposal(first_pid), [])
        revision["evidence_refs"].append(evidence[2]["id"])
        revision["end"] = claim(evidence[2], BASE + 10800000)
        with self.assertRaises(InvalidProposal):
            self.accept(proposal([revision]), evidence)
        self.assertEqual(self.count("activities"), 2)

    def test_answered_or_skipped_question_is_not_reopened_by_replay(self):
        evidence, _ = self.ingest_case("question")
        question = {"semantic_key": "test-sleep-date", "text": "1:30指哪个自然日？", "ambiguity": "跨日日期影响睡眠归属", "evidence_refs": [evidence[0]["id"]]}
        self.accept(proposal(questions=[question]), [evidence[0]])
        qid = self.store.db.execute("SELECT question_id FROM questions").fetchone()[0]
        self.assertTrue(self.store.resolve_question(qid, evidence[1]["id"], skipped=True))
        self.assertFalse(self.store.resolve_question(qid, evidence[2]["id"]))
        self.accept(proposal(questions=[question]), evidence)
        self.assertEqual(self.count("questions"), 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM questions").fetchone()[0], "skipped")

    def test_backup_api_restore_preserves_events_revisions_jobs_watermark(self):
        inputs = messages("backup")
        for message in inputs[:2]:
            self.store.ingest(message)
        evidence = self.store.evidence()
        self.accept(proposal([block(evidence[0])]))
        self.store.claim_job(["receipt"])
        with self.store.transaction() as db:
            db.execute("INSERT INTO scans VALUES(?,?,NULL,NULL,'scan_success_completeness_limited',NULL)", ("test-only-chat", BASE))
        snapshots = {}
        for table in ("messages", "observations", "activities", "jobs", "scans"):
            snapshots[table] = sorted([tuple(row) for row in self.store.db.execute("SELECT * FROM " + table)], key=repr)
        target = self.path / "independent-restore" / "backup.sqlite3"
        manifest = self.store.backup(target)
        self.assertEqual(manifest["messages"], 2)
        self.store.ingest(inputs[2])
        restored = Store(target, "test-only-app", ["test-only-owner"], ["test-only-chat"])
        self.addCleanup(restored.close)
        for table, expected in snapshots.items():
            actual = sorted([tuple(row) for row in restored.db.execute("SELECT * FROM " + table)], key=repr)
            self.assertEqual(digest(actual), digest(expected), table)
        self.assertEqual(self.count("messages"), 3)
        with self.assertRaises(ValueError):
            self.store.backup(self.store.path)

    def test_forged_evidence_id_is_rejected(self):
        evidence, _ = self.ingest_case("forged")
        item = block(evidence[0])
        item["evidence_refs"] = ["test-nonexistent-observation"]
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)

    def test_report_window_cannot_be_promoted_to_human_effort(self):
        evidence, _ = self.ingest_case("report_effort")
        item = block(evidence[0], evidence[0]["created_at_ms"], evidence[1]["created_at_ms"])
        item["evidence_refs"].append(evidence[1]["id"])
        item["start"] = claim(evidence[0], evidence[0]["created_at_ms"], "report_anchor")
        item["end"] = claim(evidence[1], evidence[1]["created_at_ms"], "report_anchor")
        validate_proposal(proposal([item]), evidence)
        item["human_intervals"] = [{"start_ms": item["start"]["value_ms"], "end_ms": item["end"]["value_ms"], "source_id": evidence[0]["id"], "quote": evidence[0]["content"]}]
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)

    def test_forged_quote_and_wrong_explicit_clock_are_rejected(self):
        evidence, _ = self.ingest_case("quote")
        item = block(evidence[0], BASE + 3600000, BASE + 7200000)
        item["start"]["quote"] = "这段原文不存在"
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)
        item["start"] = claim(evidence[0], BASE + 10800000)
        item["end"] = claim(evidence[0], BASE + 14400000)
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)

    def test_unknown_values_are_retained_and_not_declared_complete(self):
        evidence, _ = self.ingest_case("unknown")
        item = block(evidence[0])
        self.assertTrue(validate_proposal(proposal([item]), evidence))
        result = summarize([item])
        self.assertEqual(len(result["unknown_activities"]), 1)
        self.assertEqual(result["completeness"], "partial")
        item["start"]["value_ms"] = BASE
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)

    def test_negative_window_is_rejected(self):
        evidence, _ = self.ingest_case("negative")
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([block(evidence[0], BASE + 7200000, BASE + 3600000)]), evidence)

    def test_effort_cannot_extend_outside_explicit_window(self):
        evidence, _ = self.ingest_case("boundary")
        item = block(evidence[0], BASE + 3600000, BASE + 7200000, effort=True)
        item["human_intervals"][0]["end_ms"] = BASE + 7200001
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)

    def test_duration_only_does_not_manufacture_intervals(self):
        evidence, _ = self.ingest_case("duration")
        item = block(evidence[0])
        item["reported_duration_seconds"] = 83 * 60
        self.assertTrue(validate_proposal(proposal([item]), evidence))
        self.assertIsNone(item["start"]["value_ms"])
        self.assertFalse(item["human_intervals"])
        item["reported_duration_seconds"] = -1
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([item]), evidence)

    def test_proposal_question_budget_is_not_unlimited(self):
        evidence, _ = self.ingest_case("question_budget")
        questions = [{"semantic_key": "test-q-" + str(i), "text": "具体问题", "ambiguity": "具体高影响歧义", "evidence_refs": [evidence[0]["id"]]} for i in range(3)]
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal(questions=questions), evidence)

    def test_receipt_outcome_unknown_does_not_unsave_message(self):
        self.ingest_case("receipt_uncertain")
        service = self.service()
        service.platform.receipt.side_effect = PlatformError("transport_outcome_unknown")
        job = service.store.claim_job(["receipt"])
        service.process_receipt(job)
        self.assertEqual(self.count("messages"), 1)
        row = self.store.db.execute("SELECT status,client_token FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        self.assertEqual(row["status"], "outcome_unknown")
        self.assertEqual(row["client_token"], job["client_token"])

    def test_unsupported_message_types_keep_traceable_raw_evidence(self):
        evidence, _ = self.ingest_case("unsupported")
        self.assertEqual({row["message_type"] for row in evidence}, {"image", "file", "audio"})
        self.assertEqual(self.count("activities"), 0)
        self.assertEqual(self.count("messages"), 3)

    def test_wrong_timestamp_units_and_boolean_are_rejected(self):
        for message in messages("clock_units"):
            with self.subTest(message_id=message["message_id"]):
                with self.assertRaises(ValueError):
                    self.store.ingest(message)
        self.assertEqual(self.count("messages"), 0)

    def test_recall_revision_removes_previous_effort_without_losing_history(self):
        original, recall = messages("recall_projection")
        self.store.ingest(original)
        source = self.store.evidence()[0]
        first = block(source, BASE + 3600000, BASE + 7200000, effort=True)
        _, changed = self.accept(proposal([first]))
        self.store.ingest(recall)
        recalled = block(self.store.evidence()[0])
        recalled.update(activity_id=changed[0], supersedes_revision=1, status="recalled")
        self.accept(proposal([recalled]))
        self.assertEqual(self.count("activities"), 2)
        self.assertEqual(summarize(self.store.current_activities())["human_effort_seconds"], 0)
        self.assertEqual(self.store.current_activities()[0]["status"], "recalled")

    def test_http_success_with_business_error_and_429_are_failures(self):
        platform = Feishu("test-app", "TEST_FIXTURE_NOT_A_SECRET")
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"code":99991672}')):
            with self.assertRaises(PlatformError) as caught:
                platform.request("GET", "/test-only", authenticated=False)
        self.assertEqual(caught.exception.category, "business_code_99991672")
        rate = urllib.error.HTTPError("https://example.invalid/test-only", 429, "rate", {"Retry-After": "7"}, None)
        with mock.patch("urllib.request.urlopen", side_effect=rate):
            with self.assertRaises(PlatformError) as caught:
                platform.request("GET", "/test-only", authenticated=False)
        self.assertEqual(caught.exception.category, "rate_limited")
        self.assertEqual(caught.exception.retry_after, 7)


if __name__ == "__main__":
    unittest.main()
