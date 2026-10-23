"""Unit coverage for the runner's one-round child repair path."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(FIXTURES))
import run as runner  # noqa: E402


def cli_events(marker: str, source_path: str, *, label: str, include_marker: bool = True) -> str:
    agent_id = f"agent-{label}"
    child_prompt = f"Implement the fixture contract. Completion marker: {marker}\n{source_path}"
    events = [
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": agent_id, "name": "Agent",
            "input": {"subagent_type": runner.CHILD_AGENT_NAME,
                      "run_in_background": False, "prompt": child_prompt},
        }]}},
        {"type": "assistant", "parent_tool_use_id": agent_id,
         "message": {"content": [{"type": "tool_use", "id": f"edit-{label}", "name": "Edit",
                                    "input": {"file_path": source_path}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": agent_id,
                                                      "content": f"Changed {source_path}.\n{marker if include_marker else ""}"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": f"Child completed. {marker if include_marker else ''}"},
    ]
    return "\n".join(json.dumps(event) for event in events)


class ChildRepairTests(unittest.TestCase):
    def test_gateway_key_uses_project_dotenv_without_docker_control(self):
        with tempfile.TemporaryDirectory(prefix="qwen-gateway-key-test-") as temporary:
            root = Path(temporary)
            (root / ".env").write_text("GATEWAY_MASTER_KEY='test-key'\n", encoding="utf-8")
            with mock.patch.object(runner, "ROOT", root), \
                    mock.patch.object(runner.subprocess, "run",
                                      side_effect=subprocess.TimeoutExpired("docker compose exec", 15)), \
                    mock.patch.dict(runner.os.environ, {"GATEWAY_MASTER_KEY": ""}):
                self.assertEqual(runner.gateway_key(), "test-key")
                runner.subprocess.run.assert_not_called()

    def test_staging_policy_explicitly_selects_qwen_and_preserves_locked_advisor(self):
        response = mock.Mock()
        response.json.return_value = {"advisor_model_locked": True}
        client = mock.Mock()
        client.get.return_value = response
        state = {"active_model": "mimo-v2.6-pro", "mode": "force",
                 "reasoning_effort": None, "fast_mode": False,
                 "advisor_model": "gemini-subscription-argon",
                 "advisor_reasoning_effort": None, "advisor_sensitivity": "frequent"}

        runner.set_policy(client, state, test=True)

        client.get.assert_called_once_with("/api/routing-options")
        client.post.assert_called_once_with("/api/active-model", json={
            "model": "qwen3.8-27b", "mode": "alias",
            "reasoning_effort": None, "fast_mode": False,
        })

    def test_staging_policy_disables_only_an_unlocked_advisor_and_restores_snapshot(self):
        response = mock.Mock()
        response.json.return_value = {"advisor_model_locked": False}
        client = mock.Mock()
        client.get.return_value = response
        state = {"active_model": "mimo-v2.6-pro", "mode": "force",
                 "reasoning_effort": None, "fast_mode": False,
                 "advisor_model": "gemini-subscription-argon",
                 "advisor_reasoning_effort": "low", "advisor_sensitivity": "frequent"}

        runner.set_policy(client, state, test=True)
        runner.set_policy(client, state, test=False)

        self.assertEqual(client.post.call_args_list, [
            mock.call("/api/active-model", json={"model": "qwen3.8-27b", "mode": "alias",
                                                  "reasoning_effort": None, "fast_mode": False}),
            mock.call("/api/advisor-model", json={"advisor_model": None, "reasoning_effort": None,
                                                   "advisor_sensitivity": "frequent"}),
            mock.call("/api/active-model", json={"model": "mimo-v2.6-pro", "mode": "force",
                                                  "reasoning_effort": None, "fast_mode": False}),
            mock.call("/api/advisor-model", json={"advisor_model": "gemini-subscription-argon",
                                                   "reasoning_effort": "low",
                                                   "advisor_sensitivity": "frequent"}),
        ])

    def test_child_budget_supports_a_normal_multi_file_task(self):
        self.assertEqual(runner.CHILD_TURN_CAP, 24)
        self.assertEqual(runner.CHILD_TIMEOUT, 600)
        self.assertEqual(runner.run_task.__kwdefaults__["client_context_tokens"], 256_000)
        self.assertEqual(runner.cli_env("test-key")["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "256000")
        environment = runner.cli_env("test-key")
        client_context = int(environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"])
        client_cutoff = client_context * int(environment["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]) // 100
        self.assertGreater(client_cutoff, runner.QWEN_INPUT_TARGET)
        self.assertLess(client_cutoff, client_context)
        self.assertEqual(int(environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]), 4096)
        self.assertEqual(environment["MAX_THINKING_TOKENS"], "0")
        self.assertEqual(runner.QWEN_CONTEXT_TOKENS, 40_960)
        self.assertGreater(client_context, runner.QWEN_CONTEXT_TOKENS)
        self.assertEqual(environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], str(client_context))
        self.assertEqual(runner.cli_env("test-key", client_context_tokens=1_000_000)["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1000000")
        for invalid in (1_000_001, True, 32_767):
            with self.assertRaises(ValueError):
                runner.cli_env("test-key", client_context_tokens=invalid)
        command = runner.claude_command(dispatch_mode=runner.CHILD_MODE)
        self.assertEqual(runner.CHILD_PARENT_MODEL, runner.MODEL)
        self.assertEqual(command[command.index("--model") + 1], runner.MODEL)
        self.assertEqual(command[command.index("--max-turns") + 1], "24")

    def test_nested_paths_use_canonical_relative_names(self):
        with tempfile.TemporaryDirectory(prefix="qwen-nested-scope-test-") as temporary:
            workspace = Path(temporary)
            (workspace / "src").mkdir()
            (workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
            self.assertIn("src/domain.js", runner.fixture_files(workspace))
            self.assertNotIn("src\\domain.js", runner.fixture_files(workspace))

    def test_read_edit_children_are_not_told_to_execute_tests(self):
        for name in runner.TASKS:
            with self.subTest(task=name):
                self.assertNotRegex(runner.child_task_text(name), r"\bRun (?:the )?tests?\b")

    def test_cleanly_stopped_child_gets_one_scoped_repair_and_final_verification(self):
        with tempfile.TemporaryDirectory(prefix="qwen-child-repair-test-") as temporary:
            workspace = Path(temporary) / "cache_report"
            shutil.copytree(FIXTURES / "cache_report", workspace)
            original_child_spec = runner.child_agent_spec
            markers: list[str] = []

            def record_child_spec(name, directory, marker, repair_feedback=None):
                markers.append(marker)
                return original_child_spec(name, directory, marker, repair_feedback)

            tests = [
                subprocess.CompletedProcess([], 1, "", "baseline failed"),
                subprocess.CompletedProcess([], 1, "", "AssertionError: visible regression\n"),
                subprocess.CompletedProcess([], 0, "", "Ran 3 tests in 0.01s\n\nOK\n"),
            ]
            calls = []

            def fake_cli(command, *, task_input, cwd, env, timeout, idle_timeout=runner.CHILD_IDLE_TIMEOUT,
                         transcript_path=None, redact_values=()):
                index = len(calls)
                calls.append((command, task_input, timeout))
                source = cwd / "cache_report.py"
                source.write_text(source.read_text(encoding="utf-8") + f"\n# {index}\n", encoding="utf-8")
                marker = markers[index]
                output = cli_events(marker, str(source.resolve()), label=str(index), include_marker=index != 0)
                if index == 0:
                    output = "\n".join(
                        line for line in output.splitlines()
                        if json.loads(line).get("type") != "result"
                    )
                    return output, "", 124, "Claude Code produced no output for 240 seconds", True
                return output, "", 0, None, True

            log_output = (
                "completion() model= qwen3.8-27b\n"
                "sift_request protocol=anthropic_messages compacted=false active_tool_results_compacted=0 input_tokens=100 "
                "forwarded_tokens=100 target=32768 output_limit=4096\n"
            )

            with (
                mock.patch.object(runner, "child_agent_spec", side_effect=record_child_spec),
                mock.patch.object(runner, "unit_tests", side_effect=tests),
                mock.patch.object(runner, "holdout_tests", return_value=None),
                mock.patch.object(runner, "claude_command", return_value=["fake-claude"]),
                mock.patch.object(runner, "cli_env", return_value={}),
                mock.patch.object(runner, "run_cli_bounded", side_effect=fake_cli),
                mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, log_output, "")),
            ):
                outcome = runner.run_task("cache_report", workspace, "test-key",
                                          timeout=300, dispatch_mode=runner.CHILD_MODE)

            self.assertEqual(len(calls), 2)
            self.assertTrue(outcome["initial_attempt"]["repair_attempted"])
            self.assertFalse(outcome["initial_attempt"]["child_marker_returned"])
            self.assertFalse(outcome["initial_attempt"]["tests_passed"])
            self.assertEqual(outcome["cli_exit"], 0)
            self.assertTrue(outcome["tests_passed"])
            self.assertTrue(outcome["child_acceptance"]["passed"], outcome["child_acceptance"])
            self.assertTrue(outcome["child_acceptance"]["checks"]["sift_route_confirmed"])
            self.assertEqual(outcome["qwen_sift_requests"], 1)
            child_prompt = json.loads(runner.child_agent_spec(
                "cache_report", workspace, "QWEN_CHILD_COMPLETE_TEST"))[
                    runner.CHILD_AGENT_NAME]["prompt"]
            self.assertIn("one inspect-edit-finish pass", child_prompt)
            self.assertIn("return the required marker immediately", child_prompt)
            self.assertTrue(outcome["visible_tests_unchanged"])
            self.assertTrue(outcome["write_scope_clean"])
            self.assertEqual(outcome["changed_files"], ["cache_report.py"])


if __name__ == "__main__":
    unittest.main()
