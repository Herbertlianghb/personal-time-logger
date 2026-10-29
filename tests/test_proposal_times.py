import unittest
import test_core as fixtures
from test_core import block, proposal, timestamp
from time_logger.proposal import InvalidProposal, validate_proposal, _explicit_clock_matches


class CitedTimeTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    def source(self, text):
        self.store.ingest({"app_scope": "test-only-app", "sender_key": "test-only-owner", "chat_id": "test-only-chat",
                           "message_id": "time-expression-fixture", "content": text,
                           "created_at_ms": timestamp("2026-10-27T21:00:00+08:00")})
        return self.store.evidence()[0]

    def test_afternoon_qualifier_applies_to_both_range_endpoints(self):
        source = self.source("今天下午四点到六点一直学习。")
        candidate = block(source, timestamp("2026-10-27T16:00:00+08:00"), timestamp("2026-10-27T18:00:00+08:00"), effort=True)
        self.assertTrue(validate_proposal(proposal([candidate]), [source]))

    def test_today_citation_cannot_support_yesterdays_clock(self):
        source = self.source("今天10:00到11:00一直学习。")
        candidate = block(source, timestamp("2026-10-26T10:00:00+08:00"), timestamp("2026-10-26T11:00:00+08:00"))
        with self.assertRaises(InvalidProposal):
            validate_proposal(proposal([candidate]), [source])

    def test_compact_user_clock_keeps_exact_hours_and_minutes(self):
        for text, expected in (("1348上厕所", "13:48"), ("1750出门吃饭", "17:50"), ("1900去洗的", "19:00"), ("2029暂停", "20:29")):
            with self.subTest(text=text):
                value = timestamp("2026-10-27T" + expected + ":00+08:00")
                self.assertTrue(_explicit_clock_matches(value, text, {"content": text}, "minute"))
                self.assertFalse(_explicit_clock_matches(value + 60000, text, {"content": text}, "minute"))

    def test_year_date_and_money_are_not_compact_clocks(self):
        for text in ("2026年", "2026-10-27", "用了1348元", "1750分钟"):
            self.assertFalse(_explicit_clock_matches(timestamp("2026-10-27T20:26:00+08:00"), text, {"content": text}, "minute"))

    def test_report_anchor_requires_the_cited_transition_quote(self):
        source = self.source("回来了背单词")
        candidate = block(source)
        candidate["start"] = {"value_ms": source["created_at_ms"], "kind": "report_anchor",
                              "precision": "millisecond", "source_id": source["id"],
                              "quote": "回来了背单词", "date_resolution": "contextual"}
        self.assertTrue(validate_proposal(proposal([candidate]), [source]))
        for quote in ("", "现在去洗澡"):
            with self.subTest(quote=quote):
                candidate["start"]["quote"] = quote
                with self.assertRaisesRegex(InvalidProposal, "time_quote_not_in_evidence"):
                    validate_proposal(proposal([candidate]), [source])
