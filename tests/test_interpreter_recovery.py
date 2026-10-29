"""Exercise real schema/provenance checks with only the CLI boundary replaced."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from test_core import block, timestamp
from time_logger.model_runner import CodexInterpreter, InvalidModelProposal


class InterpreterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "test-clock-source", "current_observation_id": "test-clock-source",
            "kind": "receive", "source": "ws", "message_type": "text",
            "content": "练习到9：15，然后办理卡片到2205。",
            "created_at_ms": timestamp("2026-10-28T22:10:00+08:00"),
        }
        item = block(self.source, end=timestamp("2026-10-28T21:15:00+08:00"))
        item["end"]["quote"] = "练习到9：15"
        self.invalid = {"version": 1, "blocks": [item], "questions": [], "question_answers": []}
        self.estimated = copy.deepcopy(self.invalid)
        self.estimated["blocks"][0]["end"].update(kind="estimate", precision="approximate")
        self.estimated["blocks"][0]["uncertainty"] = "晚上21:15为上下文推断，原文未明示上午或下午。"
        self.inputs = []
        self.timeouts = []
        self.runner = CodexInterpreter()

    def cli(self, proposals):
        outputs = iter(proposals)

        def run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "codex-cli 0.163.0\n", "")
            self.inputs.append(json.loads(kwargs["input"].split("INPUT_JSON_DATA:\n", 1)[1]))
            self.timeouts.append(kwargs["timeout"])
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(next(outputs), ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}\n', "")
        return run

    def interpret(self, proposals):
        with mock.patch.object(self.runner, "_command_prefix", return_value=["test-codex"]), \
                mock.patch("time_logger.model_runner.subprocess.run", side_effect=self.cli(proposals)):
            return self.runner.interpret([self.source], [], [], [])

    def test_inferred_pm_is_repaired_before_import_with_exact_failure_feedback(self):
        result = self.interpret([self.invalid, self.estimated])
        self.assertEqual(result, self.estimated)
        self.assertEqual(len(self.inputs), 2)
        self.assertNotIn("validation_feedback", self.inputs[0])
        self.assertEqual(self.inputs[1]["validation_feedback"]["error_code"],
                         "explicit_time_does_not_match_cited_expression")
        self.assertEqual(self.inputs[0]["evidence"], self.inputs[1]["evidence"])
        self.assertEqual(result["blocks"][0]["human_intervals"], [])

    def test_persistently_invalid_clock_stops_after_one_repair(self):
        with self.assertRaisesRegex(InvalidModelProposal, "explicit_time_does_not_match_cited_expression"):
            self.interpret([self.invalid, self.invalid])
        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(self.runner.last_run_metadata["status"], "failed")

    def test_explicit_pm_still_passes_without_retry(self):
        self.source["content"] = "晚上9：15结束。"
        self.invalid["blocks"][0]["end"]["quote"] = "晚上9：15"
        self.assertEqual(self.interpret([self.invalid]), self.invalid)
        self.assertEqual(len(self.inputs), 1)

    def test_repair_uses_remaining_timeout_instead_of_resetting_budget(self):
        clock = [100.0]

        def cli_with_elapsed(command, **kwargs):
            result = boundary(command, **kwargs)
            if "exec" in command:
                clock[0] += 60.0
            return result

        boundary = self.cli([self.invalid, self.estimated])
        with mock.patch.object(self.runner, "_command_prefix", return_value=["test-codex"]), \
                mock.patch("time_logger.model_runner.subprocess.run", side_effect=cli_with_elapsed), \
                mock.patch("time_logger.model_runner.time.monotonic", side_effect=lambda: clock[0]):
            self.runner.interpret([self.source], [], [], [])
        self.assertEqual(self.timeouts, [180.0, 120.0])


if __name__ == "__main__":
    unittest.main()
