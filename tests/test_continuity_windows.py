"""Synthetic report/relative boundaries through provenance, projection and display."""
import copy
import json
import unittest

from tests import test_core as core
from tests import test_base_adapter as base
from tests import test_timeline as timeline
from time_logger.base_adapter import BaseAdapter
from time_logger.proposal import InvalidProposal, validate_proposal
from time_logger.timeline import TimelineReader
from time_logger.heatmap import HeatmapReader


class ContinuityWindowTests(unittest.TestCase):
    setUp = core.CoreTests.setUp

    def source(self, text, clock, *, desktop=False):
        oid = self.store.ingest({"app_scope": "test-only-app", "sender_key": "test-only-owner",
            "chat_id": "test-only-chat", "message_id": clock + text, "content": text,
            "created_at_ms": core.timestamp("2024-06-12T" + clock + "+08:00"),
            "source": "desktop" if desktop else "ws"})["observation_id"]
        return self.store.evidence([oid])[0]

    def boundary(self, source, *, anchor=None, offset=0, explicit=False):
        anchor = anchor or source
        value = anchor["created_at_ms"]
        return {"kind": "estimate", "value_ms": value + offset * 1000, "precision": "approximate",
            "source_id": source["id"], "quote": source["content"], "date_resolution": "contextual",
            "basis": {"anchor_source_id": anchor["id"], "anchor_value_ms": value,
                      "anchor_kind": "explicit_user" if explicit else "report_anchor",
                      "anchor_quote": anchor["content"], "offset_seconds": offset}}

    def accept(self, blocks):
        proposal = core.proposal(blocks)
        pid = self.store.save_proposal(proposal, self.store.evidence(), "synthetic-model", "synthetic-window")
        return self.store.accept_proposal(pid)

    def adapter(self):
        transport = base.FakeTransport()
        transport.schema["fields"][5]["options"] = [{"name": "学习"}]
        return BaseAdapter(self.store, transport, transport.schema), transport

    def activity(self, source, start, end, refs=()):
        block = core.block(source)
        block.update(name="虚构连续活动", category="学习", status="ended", start=start, end=end,
            uncertainty="估算报告窗口，非实测边界或净人工。", evidence_refs=list(dict.fromkeys([source["id"], *refs])))
        return block

    def test_completion_and_relative_ten_minutes_survive_projection_as_estimates(self):
        finished = self.source("吃完饭了", "12:00:00")
        later = self.source("吃完之后刷了10分钟，然后上厕所，现在出发", "12:40:00")
        item = self.activity(later, self.boundary(later, anchor=finished),
            self.boundary(later, anchor=finished, offset=600), [finished["id"]])
        from pathlib import Path
        from time_logger.model_runner import _check_schema
        schema = json.loads((Path(__file__).parents[1] / "config/model-proposal.schema.json").read_text())
        _check_schema({**core.proposal([item]), "question_answers": []}, schema, schema)
        aid = self.accept([item])[0]
        adapter, transport = self.adapter()
        adapter.sync(aid, 1, dry_run=False)
        row = next(iter(transport.records.values()))
        self.assertEqual(row["fields"]["开始时间"], finished["created_at_ms"])
        self.assertEqual(row["fields"]["结束时间"], finished["created_at_ms"] + 600000)
        self.assertIn("估算", row["fields"]["备注"])
        self.assertEqual(self.store.current_activities()[0]["human_intervals"], [])
        reader = TimelineReader(self.store, timeline.ReadTransport([row]), clock=lambda: later["created_at_ms"])
        detail = reader.history("2024-06-12", day_start_hour=7)
        self.assertTrue(detail["items"][0]["estimated"])
        self.assertIsNone(detail["summary"]["human_seconds"])
        heat = HeatmapReader(self.store, timeline.ReadTransport([row]), clock=lambda: later["created_at_ms"]).month("2024-06")
        day = heat["days"][11]
        self.assertEqual(day["categories"]["学习"]["estimated_seconds"], 600)
        self.assertEqual(day["categories"]["学习"], detail["recorded_totals"]["categories"]["学习"])

    def test_revision_ends_same_event_at_shared_transition_and_replay_is_stable(self):
        begin = self.source("现在开始刷词", "09:00:00")
        initial = core.block(begin)
        initial.update(status="active", start=core.claim(begin, begin["created_at_ms"], "report_anchor"))
        aid = self.accept([initial])[0]
        change = self.source("刷完了，现在去吃饭", "10:00:00")
        previous = copy.deepcopy(self.store.current_activities()[0])
        previous.update(activity_id=aid, supersedes_revision=1, status="ended",
            end=self.boundary(change), evidence_refs=[begin["id"], change["id"]], uncertainty="估算报告窗口，非净人工。")
        meal = self.activity(change, core.claim(change, change["created_at_ms"], "report_anchor"), core.unknown())
        meal["status"] = "active"
        changed = self.accept([previous, meal])
        self.assertIn(aid, changed)
        blocks = {b["activity_id"]: b for b in self.store.current_activities()}
        self.assertEqual(blocks[aid]["revision"], 2)
        next_block = next(b for key, b in blocks.items() if key != aid)
        self.assertEqual(blocks[aid]["end"]["value_ms"], next_block["start"]["value_ms"])
        adapter, transport = self.adapter()
        adapter.sync(aid, 2, dry_run=False)
        writes = transport.writes
        adapter.sync(aid, 2, dry_run=False)
        self.assertEqual(transport.writes, writes)

    def test_invalid_derivations_are_rejected_without_filling_unknowns(self):
        source = self.source("刷了10分钟，刚做完", "10:00:00")
        valid = self.activity(source, self.boundary(source, offset=-600), self.boundary(source))
        self.assertTrue(validate_proposal(core.proposal([valid]), [source]))
        changes = [lambda c: c.update(value_ms=c["value_ms"] + 1),
                   lambda c: c["basis"].update(offset_seconds=-300),
                   lambda c: c["basis"].update(anchor_source_id="missing"),
                   lambda c: c.update(precision="minute")]
        for alter in changes:
            item = copy.deepcopy(valid)
            alter(item["start"])
            with self.assertRaises(InvalidProposal):
                validate_proposal(core.proposal([item]), [source])
        item = copy.deepcopy(valid)
        item["human_intervals"] = [{"start_ms": item["start"]["value_ms"], "end_ms": item["end"]["value_ms"],
            "source_id": source["id"], "quote": source["content"]}]
        with self.assertRaisesRegex(InvalidProposal, "effort_requires_explicit_boundaries"):
            validate_proposal(core.proposal([item]), [source])

    def test_untrusted_device_and_uncertain_recollection_cannot_anchor_windows(self):
        for source in (self.source("刚刚做完", "10:00:00", desktop=True),
                       self.source("大概10:00开始？", "11:00:00")):
            item = self.activity(source, self.boundary(source), core.unknown())
            with self.assertRaises(InvalidProposal):
                validate_proposal(core.proposal([item]), [source])

    def test_cancelled_report_and_estimate_without_basis_never_become_valid_time(self):
        source = self.source("没学习，也没午休", "10:00:00")
        item = self.activity(source, self.boundary(source), self.boundary(source))
        item["status"] = "cancelled"
        aid = self.accept([item])[0]
        adapter, transport = self.adapter()
        after = adapter.sync(aid, 1)["segments"][0]["after"]
        self.assertIsNone(after["开始时间"])
        self.assertIsNone(after["结束时间"])

        item["status"] = "ended"
        for key in ("start", "end"):
            item[key].pop("basis")
        aid = self.accept([item])[0]
        after = adapter.sync(aid, 1)["segments"][0]["after"]
        self.assertIsNone(after["开始时间"])
        self.assertIsNone(after["结束时间"])

    def test_schema_is_strict_and_accepts_both_legacy_and_derived_boundaries(self):
        from pathlib import Path
        from time_logger.model_runner import _check_schema, InvalidModelProposal
        schema = json.loads((Path(__file__).parents[1] / "config/model-proposal.schema.json").read_text())
        def strict(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertEqual(set(value.get("required", [])), set(value.get("properties", {})))
                for child in value.values(): strict(child)
            elif isinstance(value, list):
                for child in value: strict(child)
        strict(schema)
        source = self.source("刚做完，用了10分钟", "10:00:00")
        plain = core.block(source)
        derived = self.activity(source, self.boundary(source, offset=-600), self.boundary(source))
        for candidate in (plain, derived):
            body = {**core.proposal([candidate]), "question_answers": []}
            _check_schema(body, schema, schema)
            bad = copy.deepcopy(body)
            bad["blocks"][0]["start"].pop("quote")
            with self.assertRaises(InvalidModelProposal):
                _check_schema(bad, schema, schema)
