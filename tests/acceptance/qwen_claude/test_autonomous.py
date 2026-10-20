"""Contract tests for generic Qwen plan/execute/verify dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import autonomous
import run as dispatch
import tool_scope_guard
import template_checkpoint


class AutonomousPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qwen-autonomous-plan-")
        self.runs_root = Path(self.temporary.name)
        self.workspace = self.runs_root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "test").mkdir()
        (self.workspace / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.workspace / "test" / "test_app.py").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def successful_inspection(self, step, *, read_count=1, search_count=0):
        path = step["files"][0]
        reads = [{"tool": "Read", "path": path, "offset": 1, "limit": 100,
                  "line_count_capped": 1}]
        for _ in range(max(0, read_count - 1)):
            read_path = "test/test_app.py" if (self.workspace / "test/test_app.py").exists() else path
            reads.append({"tool": "Read", "path": read_path, "offset": 1, "limit": 100,
                          "line_count_capped": 1})
        counts = {"Read": read_count, "Grep": search_count, "Glob": 0}
        return {
            "phase": "inspection", "success": True, "exit": 0, "stop_reason": None,
            "seconds": 1, "child_tool_calls": ["Read"], "qwen_route_calls": 1,
            "child_handoff": True, "child_result": "inspection JSON", "final": "inspected",
            "inspection_report": {"summary": "Relevant path identified",
                                  "implementation_plan": "Update the assigned implementation path.",
                                  "files": [{"path": path, "symbols": ["target"],
                                             "ranges": [{"offset": 1, "limit": 100,
                                                         "purpose": "identify the change point"}]}],
                                  "risks": []},
            "read_audit": {"read_count": read_count, "search_count": search_count,
                           "inspection_count": read_count + search_count,
                           "counts_by_tool": counts, "reads": reads,
                           "violations": [], "within_budget": True},
        }

    def test_accepts_ordered_multi_step_plan_with_more_than_two_files(self):
        plan = {
            "summary": "Implement a small vertical slice",
            "steps": [
                {"id": "step-domain", "title": "Domain", "task": "Implement domain behavior",
                 "files": ["src/app.py", "test/test_app.py", "README.md"],
                 "depends_on": [], "checks": ["Replay is idempotent"]},
                {"id": "step-ui", "title": "Expose UI", "task": "Add presentation",
                 "files": ["src/app.py"], "depends_on": ["step-domain"], "checks": []},
            ],
        }
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(len(normalized["steps"]), 2)
        self.assertEqual(len(normalized["steps"][0]["files"]), 3)

    def test_rejects_path_escape_and_excluded_paths(self):
        for path in ("../outside.py", ".git/config", "src\\app.py", "."):
            with self.subTest(path=path), self.assertRaises(ValueError):
                autonomous.relative_target(self.workspace, path)

    def test_rejects_forward_dependencies_and_oversized_step(self):
        plan = {"steps": [
            {"id": "step-1", "title": "bad", "task": "bad", "files": ["src/app.py"],
             "depends_on": ["step-2"]},
        ]}
        with self.assertRaisesRegex(ValueError, "dependency"):
            autonomous.validate_plan(plan, self.workspace)
        plan["steps"][0]["depends_on"] = [{"unexpected": "object"}]
        with self.assertRaisesRegex(ValueError, "dependency"):
            autonomous.validate_plan(plan, self.workspace)
        plan["steps"][0]["depends_on"] = []
        plan["steps"][0]["files"] = ["src/app.py", "test/test_app.py", "README.md", "new.py"]
        with self.assertRaisesRegex(ValueError, "exact files"):
            autonomous.validate_plan(plan, self.workspace, max_files_per_step=3)

    def test_rejects_test_only_implementation_steps(self):
        plan = {"steps": [{"id": "step-test-only", "title": "Add tests", "task": "Add tests",
                           "files": ["test/new-feature.test.cjs"], "depends_on": [], "checks": []}]}
        with self.assertRaisesRegex(ValueError, "test-only"):
            autonomous.validate_plan(plan, self.workspace)

    def test_requires_every_user_requirement_to_be_covered_by_plan(self):
        requirements = [{"id": "req-domain", "acceptance": "Replay is idempotent", "validator_indices": [0], "step_validator_indices": [0]},
                        {"id": "req-ui", "acceptance": "Status is visible", "validator_indices": [0], "step_validator_indices": [0]}]
        plan = {"steps": [
            {"id": "step-domain", "title": "Domain", "task": "Implement", "files": ["src/app.py"],
             "depends_on": [], "checks": ["Replay is idempotent"], "covers": ["req-domain"]},
        ]}
        with self.assertRaisesRegex(ValueError, "missing=.*req-ui"):
            autonomous.validate_plan(plan, self.workspace, requirements=requirements)
        plan["steps"][0]["covers"].append("req-ui")
        self.assertEqual(len(autonomous.validate_plan(plan, self.workspace, requirements=requirements)["steps"]), 1)

    def test_step_validator_mapping_is_caller_owned_and_validated(self):
        requirements = [{"id": "req-domain", "acceptance": "Domain holds",
                         "validator_indices": [0, 1], "step_validator_indices": [1]}]
        plan = {"steps": [{"id": "step-domain", "title": "Domain", "task": "Implement",
                           "files": ["src/app.py"], "covers": ["req-domain"],
                           "step_validator_indices": [0]}]}
        normalized = autonomous.validate_plan(plan, self.workspace, requirements=requirements,
                                              validator_count=2)
        self.assertEqual(normalized["steps"][0]["step_validator_indices"], [1])
        requirements[0]["step_validator_indices"] = [True]
        with self.assertRaisesRegex(ValueError, "invalid caller-mapped"):
            autonomous.validate_plan(plan, self.workspace, requirements=requirements, validator_count=2)

    def test_rejects_unmapped_read_only_step(self):
        requirements = [{"id": "req-change", "acceptance": "The file changes", "validator_indices": [0], "step_validator_indices": [0]}]
        plan = {"steps": [
            {"id": "step-read", "title": "Read the file", "task": "Inspect the file only",
             "files": ["src/app.py"], "depends_on": [], "checks": [], "covers": []},
            {"id": "step-write", "title": "Implement change", "task": "Append the required value",
             "files": ["src/app.py"], "depends_on": ["step-read"], "checks": [], "covers": ["req-change"]},
        ]}
        with self.assertRaisesRegex(ValueError, "must contribute"):
            autonomous.validate_plan(plan, self.workspace, requirements=requirements)

    def test_plan_cannot_assign_acceptance_tests_as_worker_edit_paths(self):
        plan = {"steps": [
            {"id": "step-1", "title": "Feature", "task": "Implement", "files": ["test/test_app.py"],
             "depends_on": [], "checks": [], "covers": []},
        ]}
        with self.assertRaisesRegex(ValueError, "read-only path"):
            autonomous.validate_plan(plan, self.workspace, read_only_paths=["test/test_app.py"])

        excluded = self.workspace / "node_modules"
        excluded.mkdir()
        dependency_alias = self.workspace / "src" / "dependency-alias"
        try:
            dependency_alias.symlink_to(excluded, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            if os.name != "nt":
                self.skipTest(f"symlink creation is unavailable: {exc}")
            junction = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(dependency_alias), str(excluded)],
                capture_output=True, text=True, check=False,
            )
            if junction.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {junction.stderr or junction.stdout}")
        with self.assertRaisesRegex(ValueError, "excluded directory"):
            autonomous.validate_plan({"steps": [{"id": "step-deps", "title": "Dependency", "task": "Implement",
                                                   "files": ["src/dependency-alias/pkg.js"],
                                                   "depends_on": [], "checks": []}]}, self.workspace)

        root_plan = {"steps": [{"id": "step-root", "title": "Root", "task": "Implement",
                                 "files": ["."], "depends_on": [], "checks": []}]}
        with self.assertRaisesRegex(ValueError, "invalid relative path|clean workspace-relative path"):
            autonomous.validate_plan(root_plan, self.workspace)

    def test_plan_cannot_target_read_only_file_through_symlink_alias(self):
        alias_dir = self.workspace / "src" / "acceptance-alias"
        try:
            alias_dir.symlink_to(self.workspace / "test", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            if os.name != "nt":
                self.skipTest(f"symlink creation is unavailable: {exc}")
            junction = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(alias_dir), str(self.workspace / "test")],
                capture_output=True, text=True, check=False,
            )
            if junction.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {junction.stderr or junction.stdout}")
        plan = {"steps": [{"id": "step-alias", "title": "Alias", "task": "Implement",
                            "files": ["src/acceptance-alias/test_app.py"], "depends_on": [], "checks": []}]}
        with self.assertRaisesRegex(ValueError, "read-only path"):
            autonomous.validate_plan(plan, self.workspace, read_only_paths=["test/test_app.py"])

    def test_plan_targets_must_be_files_and_new_file_parent_must_be_directory(self):
        directory_plan = {"steps": [{"id": "step-dir", "title": "Directory", "task": "Implement",
                                      "files": ["src"], "depends_on": [], "checks": []}]}
        with self.assertRaisesRegex(ValueError, "not a directory"):
            autonomous.validate_plan(directory_plan, self.workspace)
        (self.workspace / "src" / "blocker").write_text("file", encoding="utf-8")
        parent_file_plan = {"steps": [{"id": "step-new", "title": "New file", "task": "Implement",
                                        "files": ["src/blocker/new.py"], "depends_on": [], "checks": []}]}
        with self.assertRaisesRegex(ValueError, "missing directory"):
            autonomous.validate_plan(parent_file_plan, self.workspace)

    def test_plan_keeps_existing_supporting_references_read_only(self):
        plan = {"steps": [{"id": "step-ref", "title": "Feature", "task": "Implement",
                           "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["test/test_app.py"])
        plan["steps"][0]["files"].append("test/test_app.py")
        with self.assertRaisesRegex(ValueError, "unique and read-only"):
            autonomous.validate_plan(plan, self.workspace)
        plan["steps"][0]["files"] = ["src/app.py"]
        plan["steps"][0]["reference_paths"] = ["src/missing.js"]
        with self.assertRaisesRegex(ValueError, "references missing files"):
            autonomous.validate_plan(plan, self.workspace)

    def test_plan_validates_and_preserves_bounded_source_landmarks(self):
        plan = {"steps": [{"id": "step-source-map", "title": "Feature", "task": "Implement",
                           "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                           "source_ranges": [{"path": "src/app.py", "offset": 1, "limit": 1,
                                              "purpose": "Existing handler"}],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["source_ranges"], plan["steps"][0]["source_ranges"])
        plan["steps"][0]["source_ranges"][0]["path"] = "src/missing.py"
        with self.assertRaisesRegex(ValueError, "not an existing workspace file"):
            autonomous.validate_plan(plan, self.workspace)
        plan["steps"][0]["source_ranges"][0] = {"path": "src/app.py", "offset": 1,
                                                 "limit": 201, "purpose": "too broad"}
        with self.assertRaisesRegex(ValueError, r"limit \(1-200\)"):
            autonomous.validate_plan(plan, self.workspace)
        (self.workspace / "src" / "other.py").write_text("other = True\n", encoding="utf-8")
        plan["steps"][0]["source_ranges"][0] = {"path": "src/other.py", "offset": 1,
                                                 "limit": 1, "purpose": "wrong path"}
        with self.assertRaisesRegex(ValueError, "outside assigned and reference paths"):
            autonomous.validate_plan(plan, self.workspace)

    def test_worker_prompt_receives_source_landmarks(self):
        step = {"id": "step-domain", "title": "Domain", "task": "Implement", "files": ["src/app.py"],
                "source_ranges": [{"path": "src/app.py", "offset": 2, "limit": 4,
                                   "purpose": "Current route validation"}],
                "covers": [], "checks": []}
        prompt = json.loads(autonomous.step_agent_spec(step, "Request", [], self.workspace,
                                                       "MARKER", [], phase="inspection"))[
            autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("Caller-verified source landmarks", prompt)
        self.assertIn("src/app.py lines 2-5: Current route validation", prompt)

    def test_pretool_guard_denies_out_of_scope_and_unbounded_large_reads(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 250, encoding="utf-8")
        scope = {"root": str(self.workspace), "read_allowed": ["src/app.py", "src/large.js"],
                 "write_allowed": ["src/app.py"]}
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": str(self.workspace / "src/app.py")}, scope))
        self.assertIn("offset and limit", tool_scope_guard.authorize(
            "Read", {"file_path": str(large)}, scope))
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": str(large), "offset": 1, "limit": 200}, scope))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Grep", {"path": str(self.workspace / "src")}, scope))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Read", {"file_path": str(self.workspace / "test/test_app.py")}, scope))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Edit", {"file_path": str(self.workspace / "test/test_app.py")}, scope))

    def test_pretool_guard_fails_closed_for_workspace_escape(self):
        scope = {"root": str(self.workspace), "read_allowed": ["src/app.py"], "write_allowed": []}
        outside = self.runs_root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        self.assertIn("inside the disposable workspace", tool_scope_guard.authorize(
            "Read", {"file_path": str(outside)}, scope))

    def test_worker_tool_settings_install_scoped_pretool_hook(self):
        step = {"id": "step-test", "files": ["src/app.py"],
                "reference_paths": ["test/test_app.py"]}
        transcript = self.runs_root / "step-test-inspection.jsonl"
        settings_path = autonomous.worker_tool_settings(
            self.workspace, step, ["test/test_app.py"], "inspection", transcript)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        scope_path = transcript.with_suffix(".scope.json")
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Read|Grep|Edit|Write")
        self.assertEqual(hook["hooks"][0]["timeout"], 30)
        self.assertEqual(scope["read_allowed"], ["src/app.py", "test/test_app.py"])
        self.assertEqual(scope["write_allowed"], [])
        (self.workspace / "src" / "other.py").write_text("secret\n", encoding="utf-8")
        hook_command = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")),
                        str(scope_path)]
        denied = subprocess.run(hook_command, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": str(self.workspace / "src/other.py")},
        }), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_worker_prompt_receives_covered_requirement_acceptance(self):
        requirements = [{"id": "req-domain", "acceptance": "Replay is idempotent", "validator_indices": [0], "step_validator_indices": [0]}]
        step = {"id": "step-domain", "title": "Domain", "task": "Implement", "files": ["src/app.py"],
                "covers": ["req-domain"], "checks": []}
        prompt = json.loads(autonomous.step_agent_spec(step, "Request", requirements, self.workspace,
                                                       "MARKER", []))[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("Replay is idempotent", prompt)
        self.assertIn("req-domain", prompt)

    def test_worker_prompt_aligns_inspection_limits_and_continuation_context(self):
        step = {"id": "step-domain", "title": "Domain", "task": "Implement",
                "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                "covers": [], "checks": []}
        initial = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [],
            read_only_paths=["test/test_app.py"],
        ))[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("Read these caller-supplied acceptance and supporting reference files before editing", initial)
        self.assertIn(str((self.workspace / "test/test_app.py").resolve()), initial)
        self.assertIn("Small files may be read in full", initial)
        self.assertIn("22 Read calls", initial)
        self.assertIn("14 combined Grep/Glob calls", initial)
        self.assertIn("36 combined inspection calls", initial)
        self.assertNotIn("At most 10 Read calls", initial)

        continuation = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [],
            repair_feedback="Remaining step inspection budget: 5 Reads, 4 searches, 7 combined calls.",
            read_only_paths=["test/test_app.py"],
        ))[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("Do not reread them automatically", continuation)
        self.assertIn("Remaining step inspection budget: 5 Reads", continuation)
        self.assertIn(str((self.workspace / "test/test_app.py").resolve()), continuation)
        self.assertNotIn("Read these caller-supplied acceptance and supporting reference files before editing", continuation)

    def test_continuation_requires_remaining_inspection_budget(self):
        self.assertTrue(autonomous.has_continuation_inspection_budget(
            {"within_budget": True, "read_count": 7, "search_count": 4, "inspection_count": 11}))
        self.assertFalse(autonomous.has_continuation_inspection_budget(
            {"within_budget": True, "read_count": 8, "search_count": 2, "inspection_count": 10}))
        self.assertFalse(autonomous.has_continuation_inspection_budget(
            {"within_budget": False, "read_count": 2, "search_count": 2, "inspection_count": 4}))

    def test_repair_requires_both_step_and_implementation_phase_budget(self):
        step_audit = {"within_budget": True, "read_count": 23, "search_count": 13,
                      "inspection_count": 36, "limits": autonomous.STEP_INSPECTION_LIMITS}
        implementation_audit = {"within_budget": True, "read_count": 9, "search_count": 4,
                                "inspection_count": 13,
                                "limits": autonomous.IMPLEMENTATION_PHASE_LIMITS}
        self.assertTrue(autonomous.has_repair_inspection_budget(step_audit, implementation_audit))
        implementation_exhausted = {"within_budget": True, "read_count": 28, "search_count": 18,
                                    "inspection_count": 46}
        self.assertFalse(autonomous.has_repair_inspection_budget(step_audit, implementation_exhausted))
        self.assertIn("Remaining implementation-phase inspection budget: 19 Reads, 14 searches, 33 combined calls.",
                      self._checkpoint_resume_for_budgets(step_audit, implementation_audit))

    def _checkpoint_resume_for_budgets(self, step_audit, implementation_audit):
        path = autonomous.save_step_checkpoint(
            self.runs_root, {"id": "budget-step", "title": "Budget", "task": "Task",
                             "files": ["src/app.py"], "checks": [], "covers": []},
            1, {"success": False}, {}, {}, [], True, [], read_audit=step_audit,
            implementation_read_audit=implementation_audit,
        )
        return json.loads(path.read_text(encoding="utf-8"))["resume_instruction"]

    def test_remaining_inspection_budget_is_cumulative(self):
        audit = {"counts_by_tool": {"Read": 3, "Grep": 1, "Glob": 1}}
        self.assertEqual(autonomous.remaining_inspection_budget(audit),
                         {"reads": 5, "searches": 4, "inspections": 7})

    def test_inspection_handoff_requires_read_assigned_file_and_small_ranges(self):
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": "src/app.py", "symbols": ["route"],
                             "ranges": [{"offset": 10, "limit": 100, "purpose": "route handler"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/app.py", "offset": 1, "limit": 120}]}
        accepted = autonomous.validate_inspection_handoff(
            json.dumps(report), self.workspace, ["src/app.py"], audit)
        self.assertEqual(accepted["files"][0]["path"], "src/app.py")
        self.assertEqual(autonomous.validate_inspection_handoff(
            json.dumps(report) + "\nQWEN_AUTONOMOUS_COMPLETE_abc123", self.workspace,
            ["src/app.py"], audit)["summary"], "Found route")
        report["files"][0]["ranges"][0]["limit"] = 201
        with self.assertRaisesRegex(ValueError, "ranged evidence"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/app.py"], audit)
        report["files"][0]["ranges"][0]["limit"] = 120
        with self.assertRaisesRegex(ValueError, "not read"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/app.py"], {"reads": []})
        with self.assertRaisesRegex(ValueError, "range was not present"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/app.py"],
                {"reads": [{"tool": "Read", "path": "src/app.py"}]})

    def test_inspection_handoff_rejects_oversized_and_nonrelative_reports_clearly(self):
        with self.assertRaisesRegex(ValueError, "maximum is 6000"):
            autonomous.validate_inspection_handoff("x" * 6001, self.workspace, [], {})
        with self.assertRaisesRegex(ValueError, "exactly one JSON object"):
            autonomous.validate_inspection_handoff("Here is the report:\n{}", self.workspace, [], {})
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": str(self.workspace / "src" / "app.py"), "symbols": [],
                             "ranges": [{"offset": 1, "limit": 1, "purpose": "handler"}]}]}
        with self.assertRaisesRegex(ValueError, "invalid relative path|clean workspace-relative path"):
            autonomous.validate_inspection_handoff(json.dumps(report), self.workspace,
                                                    ["src/app.py"], {"reads": []})

    def test_preverified_inspection_must_match_declared_source_ranges(self):
        step = {"id": "step-feature", "files": ["src/app.py"], "reference_paths": [],
                "source_ranges": [{"path": "src/app.py", "offset": 1, "limit": 1,
                                   "purpose": "existing handler"}]}
        report = {"summary": "Existing handler and fixture contract inspected.",
                  "implementation_plan": "Extend the handler while preserving current behavior.",
                  "risks": [], "files": [{"path": "src/app.py", "symbols": ["handler"],
                                             "ranges": [{"offset": 1, "limit": 1,
                                                         "purpose": "existing handler"}]}]}
        accepted = autonomous.validate_preverified_inspection(report, step, self.workspace, [])
        self.assertEqual(accepted["summary"], report["summary"])
        report["files"][0]["ranges"][0]["offset"] = 2
        with self.assertRaisesRegex(ValueError, "caller-declared|range was not present"):
            autonomous.validate_preverified_inspection(report, step, self.workspace, [])

    def test_extracts_only_json_and_rejects_unstructured_output(self):
        value = autonomous.extract_json('Here is the plan:\n```json\n{"steps": []}\n```')
        self.assertEqual(value, {"steps": []})
        with self.assertRaises(ValueError):
            autonomous.extract_json("I will do that now.")

    def test_hash_diff_tracks_new_and_modified_files(self):
        before = autonomous.file_hashes(self.workspace)
        (self.workspace / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.workspace / "src" / "new.py").write_text("new = True\n", encoding="utf-8")
        self.assertEqual(autonomous.changed_paths(before, autonomous.file_hashes(self.workspace)),
                         ["src/app.py", "src/new.py"])

    def test_read_audit_bounds_large_file_reads_and_total_read_calls(self):
        large = self.workspace / "src" / "app.py"
        large.write_text("line\n" * 500, encoding="utf-8")
        read = lambda offset=None, limit=None: {
            "type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
                "input": {"file_path": str(large), **({} if offset is None else {"offset": offset, "limit": limit})}}]}}
        full = autonomous.audit_worker_reads(self.workspace, [read()])
        self.assertFalse(full["within_budget"])
        self.assertEqual(full["violations"][0]["reason"], "large_file_read_must_use_a_range_of_at_most_200_lines")
        ranged = autonomous.audit_worker_reads(self.workspace, [read(1, 200)])
        self.assertTrue(ranged["within_budget"])
        over_budget = autonomous.audit_worker_reads(self.workspace, [read(1, 200) for _ in range(11)])
        self.assertFalse(over_budget["within_budget"])
        self.assertEqual(over_budget["violations"][-1]["reason"], "file_read_budget_exceeded")
        self.assertEqual(full["reads"][0]["line_count_capped"], 401)

    def test_inspection_audit_allows_small_files_and_references_but_ranges_large_files(self):
        read = lambda path, offset=None, limit=None: {
            "type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
                "input": {"file_path": str(self.workspace / path),
                          **({} if offset is None else {"offset": offset, "limit": limit})}}]}}
        small_file = autonomous.audit_worker_reads(
            self.workspace, [read("src/app.py")], allowed_paths=["src/app.py"], require_ranged_reads=True)
        self.assertTrue(small_file["within_budget"])
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 250, encoding="utf-8")
        unbounded_large = autonomous.audit_worker_reads(
            self.workspace, [read("src/large.js")], allowed_paths=["src/large.js"], require_ranged_reads=True)
        self.assertFalse(unbounded_large["within_budget"])
        self.assertEqual(unbounded_large["violations"][0]["reason"],
                         "source_read_must_use_a_range_of_at_most_200_lines")
        read_only_reference = autonomous.audit_worker_reads(
            self.workspace, [read("test/test_app.py")],
            allowed_paths=["src/app.py", "test/test_app.py"], require_ranged_reads=True)
        self.assertTrue(read_only_reference["within_budget"])
        out_of_scope = autonomous.audit_worker_reads(
            self.workspace, [read("test/test_app.py", 1, 100)],
            allowed_paths=["src/app.py"], require_ranged_reads=True)
        self.assertFalse(out_of_scope["within_budget"])
        self.assertEqual(out_of_scope["violations"][0]["reason"],
                         "read_path_outside_assigned_step_scope")
        workspace_search = autonomous.audit_worker_reads(
            self.workspace,
            [{"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Grep",
                "input": {"path": str(self.workspace / "test/test_app.py"), "pattern": "secret"},
            }]}}],
            allowed_paths=["src/app.py", "test/test_app.py"], require_ranged_reads=True,
        )
        self.assertTrue(workspace_search["within_budget"])
        unassigned_search = autonomous.audit_worker_reads(
            self.workspace,
            [{"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Grep",
                "input": {"path": str(self.workspace / "test/test_app.py"), "pattern": "secret"},
            }]}}],
            allowed_paths=["src/app.py"],
        )
        self.assertFalse(unassigned_search["within_budget"])
        self.assertEqual(unassigned_search["violations"][0]["reason"],
                         "search_path_outside_assigned_step_scope")
        root_search = autonomous.audit_worker_reads(
            self.workspace,
            [{"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Grep",
                "input": {"path": str(self.workspace), "pattern": "secret"},
            }]}}],
            allowed_paths=["src/app.py"],
        )
        self.assertFalse(root_search["within_budget"])
        nested_excluded = self.workspace / "src" / "tmp"
        nested_excluded.mkdir()
        (nested_excluded / "secret.txt").write_text("fixture", encoding="utf-8")
        recursive_search = autonomous.audit_worker_reads(
            self.workspace,
            [{"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Grep",
                "input": {"path": str(self.workspace / "src"), "pattern": "fixture"},
            }]}}],
            allowed_paths=["src/app.py"],
        )
        self.assertFalse(recursive_search["within_budget"])
        self.assertEqual(recursive_search["violations"][0]["reason"],
                         "search_path_must_be_exact_file")
        excluded_search = autonomous.audit_worker_reads(
            self.workspace,
            [{"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Grep",
                "input": {"path": str(self.workspace / "node_modules"), "pattern": "secret"},
            }]}}],
        )
        self.assertFalse(excluded_search["within_budget"])
        self.assertEqual(excluded_search["violations"][0]["reason"],
                         "search_path_inside_excluded_directory")

    def test_audit_treats_pretool_denied_read_as_attempt_not_evidence(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 250, encoding="utf-8")
        attempted = {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "name": "Read", "id": "denied-read",
            "input": {"file_path": str(large)},
        }]}}
        denied = {"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "denied-read",
            "is_error": True,
            "content": "QWEN_SCOPE_GUARD_DENY: Files over 200 lines require Read offset and limit",
        }]}}
        audit = autonomous.audit_worker_reads(
            self.workspace, [attempted, denied], allowed_paths=["src/large.js"],
            require_ranged_reads=True)
        self.assertTrue(audit["within_budget"])
        self.assertEqual(audit["read_count"], 1)
        self.assertTrue(audit["reads"][0]["denied_before_execution"])
        successful_content = [
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Read", "id": "successful-read",
                "input": {"file_path": str(large)},
            }]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "successful-read",
                "content": "QWEN_SCOPE_GUARD_DENY: this is literal file content",
            }]}},
        ]
        successful_audit = autonomous.audit_worker_reads(
            self.workspace, successful_content, allowed_paths=["src/large.js"],
            require_ranged_reads=True)
        self.assertFalse(successful_audit["within_budget"])
        self.assertFalse(successful_audit["reads"][0]["denied_before_execution"])

    def test_read_audit_separates_reads_and_searches_but_enforces_combined_cap(self):
        app_path = str((self.workspace / "src" / "app.py").resolve())
        content = ([{"type": "tool_use", "name": "Read", "input": {"file_path": app_path}}
                   for _ in range(6)]
                  + [{"type": "tool_use", "name": "Grep", "input": {"path": app_path, "pattern": "needle"}}
                     for _ in range(6)])
        allowed = autonomous.audit_worker_reads(
            self.workspace, [{"type": "assistant", "message": {"content": content}}],
            allowed_paths=["src/app.py"])
        self.assertTrue(allowed["within_budget"])
        self.assertEqual(allowed["read_count"], 6)
        self.assertEqual(allowed["search_count"], 6)
        self.assertEqual(allowed["inspection_count"], 12)
        content.append({"type": "tool_use", "name": "Read", "input": {"file_path": app_path}})
        over_aggregate = autonomous.audit_worker_reads(
            self.workspace, [{"type": "assistant", "message": {"content": content}}],
            allowed_paths=["src/app.py"])
        self.assertFalse(over_aggregate["within_budget"])
        self.assertEqual(over_aggregate["violations"][-1]["reason"], "inspection_call_budget_exceeded")

    def test_step_read_budget_accumulates_across_continuation_attempts(self):
        first = {"counts_by_tool": {"Read": 8, "Grep": 0, "Glob": 0},
                 "reads": [{"tool": "Read", "path": f"src/{index}.js"} for index in range(8)],
                 "violations": [], "within_budget": True}
        continuation = {"counts_by_tool": {"Read": 1, "Grep": 0, "Glob": 0},
                        "reads": [{"tool": "Read", "path": "src/extra.js"}],
                        "violations": [], "within_budget": True}

        combined = autonomous.combine_read_audits(first, continuation)

        self.assertEqual(combined["read_count"], 9)
        self.assertEqual(combined["inspection_count"], 9)
        self.assertFalse(combined["within_budget"])
        self.assertEqual(combined["violations"][-1]["reason"],
                         "file_read_budget_exceeded_across_attempts")

    def test_discovery_and_implementation_have_separate_phase_budgets_and_one_step_ceiling(self):
        self.assertEqual(autonomous.CHILD_STEP_TIMEOUT, 420)
        self.assertEqual(autonomous.CHILD_IDLE_TIMEOUT, 300)
        self.assertEqual(autonomous.INSPECTION_PHASE_LIMITS,
                         {"reads": 18, "searches": 18, "inspections": 36})
        self.assertEqual(autonomous.IMPLEMENTATION_INITIAL_LIMITS,
                         {"reads": 22, "searches": 14, "inspections": 36})
        def events(reads, searches):
            content = ([{"type": "tool_use", "name": "Read", "input": {
                "file_path": str(self.workspace / "src" / "app.py"), "offset": 1, "limit": 1}}
                for _ in range(reads)] + [{"type": "tool_use", "name": "Grep", "input": {
                "path": str(self.workspace / "src" / "app.py"), "pattern": "value"}}
                for _ in range(searches)])
            return [{"type": "assistant", "message": {"content": content}}]

        inspection = autonomous.audit_worker_reads(
            self.workspace, events(15, 13), limits=autonomous.INSPECTION_PHASE_LIMITS)
        implementation = autonomous.audit_worker_reads(
                          self.workspace, events(9, 4), limits=autonomous.IMPLEMENTATION_PHASE_LIMITS)

        self.assertTrue(inspection["within_budget"])
        self.assertTrue(implementation["within_budget"])
        combined = autonomous.combine_read_audits(
            inspection, implementation, limits=autonomous.STEP_INSPECTION_LIMITS)
        self.assertTrue(combined["within_budget"])
        self.assertEqual((combined["read_count"], combined["search_count"],
                          combined["inspection_count"]), (24, 17, 41))
        too_many = autonomous.combine_read_audits(
            inspection, implementation, implementation, implementation, implementation, implementation,
            limits=autonomous.STEP_INSPECTION_LIMITS)
        self.assertFalse(too_many["within_budget"])

    def test_read_audit_fails_closed_for_external_read_and_search_paths(self):
        external = self.runs_root / "outside.txt"
        external.write_text("private", encoding="utf-8")
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(external)}},
            {"type": "tool_use", "name": "Grep", "input": {"path": str(external)}},
            {"type": "tool_use", "name": "Glob", "input": {"path": str(external)}},
            {"type": "tool_use", "name": "Edit", "input": "malformed"},
        ]}}]
        audit = autonomous.audit_worker_reads(self.workspace, events)
        self.assertFalse(audit["within_budget"])
        self.assertEqual(len(audit["violations"]), 3)
        self.assertTrue(all(item["reason"] == "read_path_outside_workspace" for item in audit["violations"]))
        self.assertNotIn(str(external), json.dumps(audit))

    def test_read_audit_counts_default_searches_and_rejects_malformed_paths(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "needle"}},
            {"type": "tool_use", "name": "Glob", "input": {"pattern": "*.py"}},
            {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "tool_use", "name": "Read", "input": "malformed"},
            {"type": "tool_use", "name": "Grep", "input": {"path": None}},
        ]}}]
        audit = autonomous.audit_worker_reads(self.workspace, events)
        self.assertEqual(audit["read_count"], 2)
        self.assertEqual([entry["path"] for entry in audit["reads"][:2]], [".", "."])
        self.assertFalse(audit["within_budget"])
        self.assertEqual(sum(item["reason"] == "invalid_read_tool_path" for item in audit["violations"]), 3)

    def test_deadline_checkpoint_records_partial_state_and_resume_contract(self):
        run_dir = self.runs_root / "checkpoints"
        before = {"src/app.py": "before-hash"}
        after = {"src/app.py": "after-hash"}
        step = {"id": "step-domain", "title": "Domain", "task": "Implement behavior",
                "files": ["src/app.py"], "checks": ["Behavior persists"], "covers": ["req-domain"]}
        path = autonomous.save_step_checkpoint(
            run_dir, step, 1,
            {"success": False, "exit": 124, "stop_reason": "deadline", "seconds": 240,
             "child_tool_calls": ["Read", "Edit"], "qwen_route_calls": 5,
             "child_handoff": False, "child_result": "partial"},
            before, after, ["src/app.py"], True, ["test/test_app.py"],
            [{"argv": ["python", "-m", "unittest"], "exit": 1,
              "stdout": "assertion failed", "stderr": ""}],
        )
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["worker"]["qwen_route_calls"], 5)
        self.assertEqual(checkpoint["after_hashes"]["src/app.py"], "after-hash")
        self.assertEqual(checkpoint["read_only_acceptance_paths"], ["test/test_app.py"])
        self.assertEqual(checkpoint["diagnostics"][0]["exit"], 1)
        self.assertIn("same persistent workspace", checkpoint["resume_instruction"])

    def test_saved_plan_is_revalidated_and_skips_planner_call(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        requirements = [{"id": "req-feature", "acceptance": "Feature is implemented",
                         "validator_indices": [0], "step_validator_indices": [0]}]
        plan = {"summary": "One scoped feature step", "steps": [
            {"id": "step-feature", "title": "Implement feature", "task": "Implement the feature",
             "files": ["src/app.py"], "depends_on": [], "checks": ["Feature check passes"],
             "covers": ["req-feature"],
             "source_ranges": [{"path": "src/app.py", "offset": 1, "limit": 1,
                                "purpose": "Existing implementation entry point"}]},
        ]}
        source_state = self.runs_root / "prior-run-state.json"
        source_state.write_text(json.dumps({
            "status": "interrupted_clean", "workspace": str(self.workspace),
            "steps": [{"id": "step-feature", "status": "accepted"}], "plan": plan,
        }), encoding="utf-8")
        loaded_plan = autonomous.load_plan_file(source_state)

        class Response:
            def __init__(self, value=None):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        active_model = {"active_model": "qwen", "mode": "alias", "reasoning_effort": "high",
                        "advisor_model": None, "advisor_reasoning_effort": None,
                        "advisor_sensitivity": "medium", "advisor_trigger": None,
                        "advisor_classifier": None, "fast_mode": False}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                return Response(active_model if path == "/api/active-model" else {})

        def child(workspace, step, request, reqs, key, previous, **kwargs):
            self.assertEqual(kwargs.get("phase"), "implementation")
            self.assertIn("Existing handler is one line", json.dumps(kwargs.get("inspection_context")))
            (workspace / "src" / "app.py").write_text("feature = True\n", encoding="utf-8")
            return {"success": True, "exit": 0, "stop_reason": None, "seconds": 1,
                    "child_tool_calls": ["Read", "Edit"], "qwen_route_calls": 1,
                    "child_handoff": True, "child_result": "implemented", "final": "complete"}

        passed = [{"argv": ["acceptance-probe"], "exit": 0, "stdout": "passed", "stderr": ""}]
        with mock.patch.object(autonomous, "run_validators", return_value=passed), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous, "plan_with_qwen", side_effect=AssertionError("planner called")), \
             mock.patch.object(autonomous, "run_child", side_effect=child):
            result = autonomous.run_request(
                self.workspace, "Implement a feature", [["acceptance-probe"]],
                runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                validator_timeout=5, baseline_validators=[["baseline-probe"]],
                requirements=requirements, plan_override=loaded_plan,
                plan_source=str(source_state),
                preverified_inspections={"step-feature": {
                    "summary": "Existing handler is one line in the assigned file.",
                    "implementation_plan": "Preserve its current behavior and add the feature in the same module.",
                    "risks": [],
                    "files": [{"path": "src/app.py", "symbols": ["handler"],
                               "ranges": [{"offset": 1, "limit": 1,
                                           "purpose": "Existing implementation entry point"}]}],
                }},
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["planning"], {
            "source": "validated_plan_file", "plan_source": str(source_state)})
        self.assertEqual(result["plan"]["steps"][0]["covers"], ["req-feature"])
        self.assertEqual(result["steps"][0]["inspection"]["source"],
                         "orchestrator_preverified_source_map")
        self.assertIsNone(result["steps"][0]["inspection"]["worker"])

    def test_run_request_stops_before_implementation_on_out_of_scope_inspection_read(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                      "validator_indices": [0], "step_validator_indices": [0]}
        plan = {"summary": "One implementation step", "steps": [{
            "id": "step-feature", "title": "Implement feature", "task": "Implement it",
            "files": ["src/app.py"], "depends_on": [], "checks": ["Feature works"],
            "covers": ["req-feature"], "step_validator_indices": [0],
        }]}

        class Response:
            def __init__(self, value=None):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        active = {"active_model": "qwen", "mode": "alias", "reasoning_effort": "high",
                  "advisor_model": None, "advisor_reasoning_effort": None,
                  "advisor_sensitivity": "medium", "advisor_trigger": None,
                  "advisor_classifier": None, "fast_mode": False}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                return Response(active if path == "/api/active-model" else {})

            def post(self, *args, **kwargs):
                return Response({})

        implementation_calls = []

        def child(workspace, step, request, requirements, key, previous, **kwargs):
            if kwargs.get("phase") == "implementation":
                implementation_calls.append(step["id"])
                raise AssertionError("implementation must wait for an in-scope inspection handoff")
            return {"phase": "inspection", "success": False, "inspection_error": None,
                    "read_audit": {"read_count": 1, "search_count": 0, "inspection_count": 1,
                                   "counts_by_tool": {"Read": 1, "Grep": 0, "Glob": 0},
                                   "reads": [{"tool": "Read", "path": "test/test_app.py",
                                              "offset": 1, "limit": 100}],
                                   "violations": [{"reason": "read_path_outside_assigned_step_scope"}],
                                   "within_budget": False}}

        passed = [{"argv": ["acceptance"], "exit": 0, "stdout": "passed", "stderr": ""}]
        with mock.patch.object(autonomous, "run_validators", return_value=passed), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous, "run_child", side_effect=child):
            result = autonomous.run_request(
                self.workspace, "Implement a feature", [["acceptance"]],
                runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                validator_timeout=5, baseline_validators=[["baseline"]],
                requirements=[requirement], plan_override=plan,
            )

        self.assertEqual(result["status"], "stopped_inspection_failure")
        self.assertEqual(implementation_calls, [])
        self.assertEqual(result["steps"][0]["inspection"]["status"], "failed")

    def test_run_request_propagates_acceptance_paths_and_diagnostics_through_recovery(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        requirements = [{"id": "req-feature", "acceptance": "The feature is persisted",
                         "validator_indices": [0], "step_validator_indices": [0]}]
        step = {"id": "step-feature", "title": "Persist feature", "task": "Persist the feature",
                "files": ["src/app.py"], "depends_on": [], "checks": ["Feature persists"],
                "covers": ["req-feature"], "step_validator_indices": [0]}
        plan = {"summary": "One feature step", "steps": [step]}
        read_only = ["test/test_app.py"]

        class Response:
            def __init__(self, value=None):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                if path == "/api/active-model":
                    return Response({"active_model": "qwen", "mode": "alias",
                                     "reasoning_effort": "high", "advisor_model": None,
                                     "advisor_reasoning_effort": None, "advisor_sensitivity": "medium",
                                     "advisor_trigger": None, "advisor_classifier": None,
                                     "fast_mode": False})
                return Response({})

            def post(self, *args, **kwargs):
                return Response({})

        validation_calls = []

        def validators(workspace, argv, **kwargs):
            validation_calls.append(argv)
            # Baseline passes, first step check fails, continuation fixes it,
            # final acceptance fails once, and final repair validation passes.
            if len(validation_calls) == 2:
                return [{"argv": argv[0], "exit": 1, "stdout": "assertion failed", "stderr": "details"}]
            if len(validation_calls) == 4:
                return [{"argv": argv[0], "exit": 1, "stdout": "integration failed", "stderr": ""}]
            return [{"argv": argv[0], "exit": 0, "stdout": "passed", "stderr": ""}]

        child_calls = []
        implementation_calls = 0

        def child(workspace, child_step, request, reqs, key, previous, **kwargs):
            child_calls.append((child_step, kwargs.copy()))
            if kwargs.get("phase") == "inspection":
                return self.successful_inspection(child_step, read_count=13, search_count=15)
            if child_step["id"] == "step-final-integration-repair":
                target = workspace / "src" / "app.py"
                target.write_text(target.read_text(encoding="utf-8") + "# final integration repair\n",
                                  encoding="utf-8")
                return {"success": True, "exit": 0, "stop_reason": None, "seconds": 1,
                        "child_tool_calls": ["Read", "Edit"], "qwen_route_calls": 1,
                        "child_handoff": True, "child_result": "integrated", "final": "complete",
                        "read_audit": {}}
            nonlocal implementation_calls
            implementation_calls += 1
            if kwargs.get("repair_feedback"):
                checkpoint_line = next(line for line in kwargs["repair_feedback"].splitlines()
                                       if line.startswith("Checkpoint: "))
                checkpoint = json.loads(Path(checkpoint_line.removeprefix("Checkpoint: ")).read_text(
                    encoding="utf-8"))
                self.assertEqual(checkpoint["diagnostics"][0]["exit"], 1)
                resume = checkpoint["resume_instruction"]
                self.assertIn("Remaining step inspection budget: 24 Reads, 17 searches, 41 combined calls.", resume)
                self.assertIn("Remaining implementation-phase inspection budget: 19 Reads, 14 searches, 33 combined calls.", resume)
                self.assertIn("test/test_app.py", resume)
                self.assertNotIn("Read the listed acceptance paths first", resume)
                self.assertNotIn("Read the acceptance files first", kwargs["repair_feedback"])
                self.assertIn("24 Reads, 17 searches, 41 combined calls", kwargs["repair_feedback"])
            target = workspace / "src" / "app.py"
            target.write_text(target.read_text(encoding="utf-8") + f"# attempt {implementation_calls}\n",
                              encoding="utf-8")
            read_audit = ({"read_count": 9, "search_count": 4, "inspection_count": 13,
                           "counts_by_tool": {"Read": 9, "Grep": 4, "Glob": 0},
                           "reads": [{"tool": "Read", "path": "src/app.py"} for _ in range(9)],
                           "violations": [], "within_budget": True}
                          if implementation_calls == 1 else {})
            return {"success": implementation_calls != 1, "exit": 0 if implementation_calls != 1 else 124,
                    "stop_reason": "deadline" if implementation_calls == 1 else None,
                    "seconds": 1, "child_tool_calls": ["Read", "Edit"], "qwen_route_calls": 2,
                    "child_handoff": implementation_calls != 1, "child_result": "partial edit",
                    "final": "complete" if implementation_calls != 1 else None,
                    "read_audit": read_audit}

        with mock.patch.object(autonomous, "run_validators", side_effect=validators), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous, "plan_with_qwen", return_value=(plan, {"seconds": 1})), \
             mock.patch.object(autonomous, "run_child", side_effect=child):
            result = autonomous.run_request(
                self.workspace, "Implement a persisted feature", [["acceptance-probe"]],
                runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                validator_timeout=5, baseline_validators=[["baseline-probe"]],
                requirements=requirements, read_only_paths=read_only,
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(child_calls), 4)
        self.assertEqual(result["steps"][0]["handoff"]["status"], "received")
        self.assertEqual(result["steps"][0]["worker_protocol"]["status"], "passed")
        inspection_kwargs = child_calls[0][1]
        first_kwargs = child_calls[1][1]
        continuation_kwargs = child_calls[2][1]
        final_repair_kwargs = child_calls[3][1]
        self.assertEqual(inspection_kwargs["phase"], "inspection")
        self.assertEqual(first_kwargs["phase"], "implementation")
        self.assertIsNotNone(first_kwargs["inspection_context"])
        self.assertEqual(first_kwargs["inspection_limits"], {"reads": 22, "searches": 14, "inspections": 36})
        self.assertEqual(continuation_kwargs["inspection_limits"], {"reads": 19, "searches": 14, "inspections": 33})
        self.assertEqual(first_kwargs["read_only_paths"], read_only)
        self.assertEqual(continuation_kwargs["read_only_paths"], read_only)
        self.assertIn("Checkpoint:", continuation_kwargs["repair_feedback"])
        self.assertIn("assertion failed", continuation_kwargs["repair_feedback"])
        checkpoint_path = Path(continuation_kwargs["repair_feedback"].split("Checkpoint: ", 1)[1].splitlines()[0])
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["read_only_acceptance_paths"], read_only)
        self.assertEqual(checkpoint["diagnostics"][0]["exit"], 1)
        self.assertEqual(checkpoint["read_audit"]["read_count"], 22)
        self.assertEqual(checkpoint["read_audit"]["search_count"], 19)
        self.assertEqual(checkpoint["read_audit"]["inspection_count"], 41)
        self.assertEqual(checkpoint["read_audit"]["limits"], {"reads": 46, "searches": 36, "inspections": 82})
        self.assertEqual(final_repair_kwargs["read_only_paths"], read_only)
        self.assertIn("integration failed", final_repair_kwargs["repair_feedback"])
        self.assertEqual(validation_calls[0], [["baseline-probe"]])
        self.assertEqual(validation_calls[1], [["acceptance-probe"]])

    def test_run_child_serializes_acceptance_paths_and_repair_feedback(self):
        step = {"id": "step-feature", "title": "Persist feature", "task": "Persist the feature",
                "files": ["src/app.py"], "covers": ["req-feature"], "checks": ["Feature persists"]}
        requirements = [{"id": "req-feature", "acceptance": "The feature is persisted"}]
        repair_feedback = "Checkpoint: checkpoint.json\nDiagnostic: assertion failed"
        cli_call = {}

        def fake_cli(command, **kwargs):
            cli_call.update(kwargs)
            marker = next(line.removeprefix("Marker: ") for line in kwargs["task_input"].splitlines()
                          if line.startswith("Marker: "))
            events = [
                "unexpected JSON scalar event",
                {"type": "assistant", "message": {"content": "unexpected scalar content"}},
                {"type": "user", "message": {"content": "unexpected scalar content"}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                                     "id": "child-1"}]}},
                {"type": "assistant", "parent_tool_use_id": "child-1", "message": {"content": [
                    {"type": "tool_use", "name": "Edit",
                     "input": {"file_path": str((self.workspace / "src/app.py").resolve())}},
                ]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "child-1",
                                                                 "content": f"done {marker}"}]}},
                {"type": "result", "subtype": "success", "result": "child complete"},
            ]
            return "\n".join(json.dumps(event) for event in events), "", 0, None, True

        with mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]) as command, \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["docker", "logs"], 20)):
            result = autonomous.run_child(
                self.workspace, step, "Implement the feature", requirements, "test-key", [], timeout=5,
                transcript_path=self.runs_root / "worker.jsonl", repair_feedback=repair_feedback,
                read_only_paths=["test/test_app.py"],
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["telemetry"]["status"], "unavailable")
        self.assertIsNone(result["qwen_route_calls"])
        agent_spec = json.loads(command.call_args.kwargs["agents"])[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn(str((self.workspace / "test/test_app.py").resolve()), agent_spec)
        self.assertIn("Diagnostic: assertion failed", agent_spec)
        self.assertIn("Do not reread them automatically", agent_spec)
        self.assertNotIn("Read these caller-supplied acceptance and supporting reference files before editing", agent_spec)
        self.assertIn("22 Read calls", agent_spec)
        self.assertEqual(cli_call["cwd"], self.workspace)

    def test_ejs_repair_keeps_batch_hook_without_requiring_a_second_template_checkpoint(self):
        template = self.workspace / "views" / "page.ejs"
        template.parent.mkdir()
        template.write_text("<p>ready</p>\n", encoding="utf-8")
        test_file = self.workspace / "test" / "test_page.cjs"
        test_file.write_text("// old expectation\n", encoding="utf-8")
        step = {"id": "step-ui", "title": "Fix rendered selection", "task": "Fix the assigned view",
                "files": ["views/page.ejs", "test/test_page.cjs"], "covers": [], "checks": []}
        cli_call = {}

        def fake_cli(command, **kwargs):
            cli_call.update(kwargs)
            marker = next(line.removeprefix("Marker: ") for line in kwargs["task_input"].splitlines()
                          if line.startswith("Marker: "))
            test_file.write_text("// corrected ordering assertion\n", encoding="utf-8")
            events = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                                       "id": "repair-child"}]}},
                {"type": "assistant", "parent_tool_use_id": "repair-child", "message": {"content": [
                    {"type": "tool_use", "name": "Edit", "id": "edit-test",
                     "input": {"file_path": str(test_file.resolve())}},
                ]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "edit-test",
                                                                    "content": "test expectation updated"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result",
                                                                    "tool_use_id": "repair-child",
                                                                    "content": f"done {marker}"}]}},
                {"type": "result", "subtype": "success", "result": "repair complete"},
            ]
            return "\n".join(json.dumps(event) for event in events), "", 0, None, True

        with mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]) as command, \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["docker", "logs"], 20)):
            result = autonomous.run_child(
                self.workspace, step, "Repair the focused render failure", [], "test-key", [], timeout=5,
                transcript_path=self.runs_root / "repair.jsonl",
                repair_feedback="The remaining failure is test ordering.",
                checkpoint_validators=[["node", "scripts/check-ejs.js"]],
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["changed_tool_paths"], [str(test_file.resolve())])
        self.assertIsNone(cli_call["checkpoint_deadline"])
        settings_path = command.call_args.kwargs["settings"]
        settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        self.assertEqual(len(settings["hooks"]["PostToolBatch"]), 1,
                         "an EJS edit during repair should still receive syntax feedback")
        checkpoint = json.loads((self.runs_root / "repair.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["validators"], [["node", "scripts/check-ejs.js"]])

    def test_run_child_inspection_phase_is_read_only_and_requires_source_ranges(self):
        step = {"id": "step-feature", "title": "Persist feature", "task": "Persist the feature",
                "files": ["src/app.py"], "covers": ["req-feature"], "checks": ["Feature persists"]}
        requirements = [{"id": "req-feature", "acceptance": "The feature is persisted"}]
        spec_holder = {}

        def make_command(**kwargs):
            spec_holder.update(kwargs)
            return ["claude"]

        def fake_cli(command, **kwargs):
            worker = json.loads(spec_holder["agents"])[autonomous.AUTONOMOUS_CHILD]
            marker = worker["prompt"].split("End with ", 1)[1].split(" ", 1)[0]
            report = {"summary": "The source has a single state variable.",
                      "implementation_plan": "Update the existing state assignment.",
                      "risks": [], "files": [{"path": "src/app.py", "symbols": ["value"],
                          "ranges": [{"offset": 1, "limit": 100,
                                      "purpose": "locate the assignment"}]}]}
            events = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                                     "id": "child-inspect"}]}},
                {"type": "assistant", "parent_tool_use_id": "child-inspect", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "id": "read-1",
                     "input": {"file_path": str((self.workspace / "src/app.py").resolve()),
                               "offset": 1, "limit": 100}},
                ]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read-1",
                                                               "content": "value = 1"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "child-inspect",
                                                               "content": json.dumps(report) + "\n" + marker}]}},
                {"type": "result", "subtype": "success", "result": "inspection complete"},
            ]
            return "\n".join(json.dumps(event) for event in events), "", 0, None, True

        before = autonomous.file_hashes(self.workspace)
        with mock.patch.object(autonomous.dispatch, "claude_command", side_effect=make_command), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["docker", "logs"], 20)):
            result = autonomous.run_child(
                self.workspace, step, "Implement the feature", requirements, "test-key", [], timeout=5,
                transcript_path=self.runs_root / "inspection.jsonl", phase="inspection",
            )

        definition = json.loads(spec_holder["agents"])[autonomous.AUTONOMOUS_CHILD]
        self.assertEqual(definition["tools"], ["Read", "Grep"])
        self.assertTrue(result["success"])
        self.assertEqual(result["inspection_report"]["files"][0]["path"], "src/app.py")
        self.assertEqual(result["read_audit"]["inspection_count"], 1)
        self.assertEqual(autonomous.file_hashes(self.workspace), before)

    def test_nested_excluded_paths_are_rejected_consistently(self):
        nested = self.workspace / "src" / "tmp"
        nested.mkdir()
        (nested / "generated_by_worker.py").write_text("x = 1\n", encoding="utf-8")
        self.assertNotIn("src/tmp/generated_by_worker.py", autonomous.workspace_files(self.workspace))
        with self.assertRaisesRegex(ValueError, "excluded directory"):
            autonomous.relative_target(self.workspace, "src/tmp/generated_by_worker.py")

    def test_generated_output_paths_are_excluded_consistently(self):
        generated = self.workspace / ".runs" / "worker-output.json"
        generated.parent.mkdir()
        generated.write_text("{}", encoding="utf-8")
        self.assertNotIn(".runs/worker-output.json", autonomous.workspace_files(self.workspace))
        self.assertIn(".runs/worker-output.json", autonomous.file_hashes(self.workspace))

    def test_refuses_workspace_without_disposable_manifest(self):
        with self.assertRaisesRegex(ValueError, "disposable"):
            autonomous.assert_disposable_workspace(self.workspace, self.runs_root)
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        self.assertTrue(autonomous.assert_disposable_workspace(self.workspace, self.runs_root))

    def test_refuses_marked_primary_outside_runner_scratch_root(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nested under the runner scratch root"):
            autonomous.assert_disposable_workspace(self.workspace, self.workspace)

    def test_interrupted_run_is_reconciled_and_never_auto_resumed(self):
        state_path = self.workspace / "state.json"
        before = autonomous.file_hashes(self.workspace)
        state = {"workspace": str(self.workspace.resolve()), "status": "executing",
                 "initial_hashes": before, "steps": [{"status": "running",
                 "attempts": [{"before_hashes": before}]}]}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (self.workspace / "src" / "app.py").write_text("partial = True\n", encoding="utf-8")
        result = autonomous.reconcile_interrupted_state(state_path, self.workspace)
        self.assertEqual(result["status"], "interrupted_dirty")
        self.assertFalse(result["resume_allowed"])

    def test_child_cli_inherits_write_only_when_explicitly_requested(self):
        settings_file = self.runs_root / "worker-settings.json"
        settings_file.write_text("{}", encoding="utf-8")
        command = dispatch.claude_command(dispatch_mode=dispatch.CHILD_MODE, child_tools="Read,Edit,Write",
                                          settings=str(settings_file))
        self.assertIn("--restricted", command)
        self.assertEqual(command[command.index("--settings") + 1], str(settings_file))
        self.assertNotIn("--add-dir", command)
        parent_tools = command[command.index("--tools") + 1]
        allowed_tools = command[command.index("--allowedTools") + 1]
        self.assertEqual(parent_tools, "Agent,Read,Edit,Write")
        self.assertEqual(allowed_tools, "Agent,Read,Edit,Write")
        inspection_command = dispatch.claude_command(
            dispatch_mode=dispatch.CHILD_MODE, child_tools="Read,Grep")
        self.assertEqual(inspection_command[inspection_command.index("--tools") + 1], "Agent,Read,Grep")
        self.assertEqual(inspection_command[inspection_command.index("--allowedTools") + 1], "Agent,Read,Grep")
        implementation_command = dispatch.claude_command(
            dispatch_mode=dispatch.CHILD_MODE, child_tools="Read,Grep,Edit,Write")
        self.assertEqual(implementation_command[implementation_command.index("--tools") + 1],
                         "Agent,Read,Grep,Edit,Write")
        self.assertEqual(implementation_command[implementation_command.index("--allowedTools") + 1],
                         "Agent,Read,Grep,Edit,Write")
        with self.assertRaises(ValueError):
            dispatch.claude_command(dispatch_mode=dispatch.CHILD_MODE, child_tools="Bash,Read")

    def test_planner_cli_has_no_tools_instead_of_relying_on_prompt_compliance(self):
        command = dispatch.claude_command(tools="", max_turns=2, model=dispatch.MODEL, bare=True)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--allowedTools") + 1], "")

    def test_planner_prompt_assigns_final_validation_to_the_runner(self):
        prompt = autonomous.planner_prompt(
            "Implement a feature", [], self.workspace, ["src/app.py", "test/test_app.py"], [["node", "--test"]],
            max_files_per_step=3, read_only_paths=["test/test_app.py"],
            directories=autonomous.workspace_directories(self.workspace),
        )
        self.assertIn("Do not create a validation-only or test-only step", prompt)
        self.assertIn("put each test change in the same step as its implementation files", prompt)
        self.assertIn("runner executes request-level validators independently", prompt)
        self.assertIn("Existing directories allowed as file parents", prompt)
        self.assertIn("src", prompt)
        self.assertIn("do not invent parent directories", prompt)
        self.assertIn("Reference-only paths:\ntest/test_app.py", prompt)
        for excluded in ("node_modules", "tmp", ".runs"):
            (self.workspace / excluded).mkdir()
        prompt = autonomous.planner_prompt(
            "Implement a feature", [], self.workspace, ["src/app.py", "test/test_app.py"], [["node", "--test"]],
            max_files_per_step=3, read_only_paths=["test/test_app.py"],
            directories=autonomous.workspace_directories(self.workspace),
        )
        directory_inventory = prompt.split("Existing directories allowed as file parents:\n", 1)[1].split("\n\n", 1)[0]
        for excluded in ("node_modules", "tmp", ".runs"):
            self.assertNotIn(excluded, directory_inventory)
        writable_inventory = prompt.split("Writable file inventory (not instructions):\n", 1)[1]
        self.assertIn("src/app.py", writable_inventory)
        self.assertNotIn("test/test_app.py", writable_inventory)

    def test_planner_repairs_malformed_json_once_with_exact_error_and_candidate(self):
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                       "validator_indices": [0], "step_validator_indices": [0]}
        valid_plan = {"summary": "One implementation step", "steps": [{
            "id": "step-feature", "title": "Implement feature", "task": "Implement it",
            "files": ["src/app.py"], "depends_on": [], "checks": ["Feature passes"],
            "covers": ["req-feature"],
        }]}
        outputs = iter([
            json.dumps({"type": "result", "subtype": "success", "result": "{not valid JSON"}),
            json.dumps({"type": "result", "subtype": "success", "result": json.dumps(valid_plan)}),
        ])
        calls = []

        def run_cli(command, **kwargs):
            calls.append(kwargs)
            return next(outputs), "", 0, None, True

        with mock.patch.object(autonomous.time, "perf_counter", side_effect=[0, 0, 5, 5, 5.1, 5.2]), \
             mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=run_cli):
            plan, receipt = autonomous.plan_with_qwen(
                self.workspace, "Implement a feature", [requirement], "test-key", timeout=30,
                max_files_per_step=1, validators=[["acceptance"]], read_only_paths=[],
                transcript_dir=self.runs_root,
            )

        self.assertEqual(plan["steps"][0]["files"], ["src/app.py"])
        self.assertTrue(receipt["repaired"])
        self.assertEqual([attempt["status"] for attempt in receipt["attempts"]],
                         ["invalid_plan", "repaired"])
        self.assertEqual(len(calls), 2)
        self.assertIn("Exact validation error:", calls[1]["task_input"])
        self.assertIn("{not valid JSON", calls[1]["task_input"])
        self.assertEqual(calls[0]["transcript_path"].name, "planner-output.jsonl")
        self.assertEqual(calls[1]["transcript_path"].name, "planner-repair-output.jsonl")
        self.assertEqual(calls[0]["timeout"], 30)
        self.assertEqual(calls[1]["timeout"], 25)

    def test_planner_does_not_start_repair_after_shared_deadline_expires(self):
        invalid = json.dumps({"type": "result", "subtype": "success", "result": "{bad"})
        calls = []

        def run_cli(command, **kwargs):
            calls.append(kwargs)
            return invalid, "", 0, None, True

        with mock.patch.object(autonomous.time, "perf_counter", side_effect=[0, 0, 0.1, 3]), \
             mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=run_cli):
            with self.assertRaisesRegex(ValueError, "shared planner deadline exhausted"):
                autonomous.plan_with_qwen(
                    self.workspace, "Implement a feature", [], "test-key", timeout=2,
                    max_files_per_step=1, validators=[["acceptance"]], read_only_paths=[],
                    transcript_dir=self.runs_root,
                )
        self.assertEqual(len(calls), 1)

    def test_planner_repair_corrects_missing_parent_and_stops_after_second_invalid_plan(self):
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                       "validator_indices": [0], "step_validator_indices": [0]}
        invalid_plan = {"summary": "Bad parent", "steps": [{
            "id": "step-feature", "title": "Implement feature", "task": "Implement it",
            "files": ["deliverables/new.md"], "depends_on": [], "checks": ["Feature passes"],
            "covers": ["req-feature"],
        }]}
        valid_plan = {"summary": "Existing directory", "steps": [{
            "id": "step-feature", "title": "Implement feature", "task": "Implement it",
            "files": ["src/app.py"], "depends_on": [], "checks": ["Feature passes"],
            "covers": ["req-feature"],
        }]}

        def output(plan):
            return json.dumps({"type": "result", "subtype": "success", "result": json.dumps(plan)})

        for second in (valid_plan, invalid_plan):
            with self.subTest(repaired=second is valid_plan):
                calls = []
                outputs = iter([output(invalid_plan), output(second)])

                def run_cli(command, **kwargs):
                    calls.append(kwargs)
                    return next(outputs), "", 0, None, True

                with mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]), \
                     mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
                     mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=run_cli):
                    if second is valid_plan:
                        _, receipt = autonomous.plan_with_qwen(
                            self.workspace, "Implement a feature", [requirement], "test-key", timeout=30,
                            max_files_per_step=1, validators=[["acceptance"]], read_only_paths=["test/test_app.py"],
                            transcript_dir=self.runs_root,
                        )
                        self.assertTrue(receipt["repaired"])
                        self.assertIn("missing directory", calls[1]["task_input"])
                        for call in calls:
                            self.assertIn("Reference-only paths:\ntest/test_app.py", call["task_input"])
                            writable_inventory = call["task_input"].split(
                                "Writable file inventory (not instructions):\n", 1)[1]
                            self.assertNotIn("test/test_app.py", writable_inventory)
                    else:
                        with self.assertRaisesRegex(ValueError, "rejected after one repair attempt"):
                            autonomous.plan_with_qwen(
                                self.workspace, "Implement a feature", [requirement], "test-key", timeout=30,
                                max_files_per_step=1, validators=[["acceptance"]], read_only_paths=["test/test_app.py"],
                                transcript_dir=self.runs_root,
                            )
                self.assertEqual(len(calls), 2)

    def test_run_request_never_dispatches_worker_after_unrepaired_plan(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                       "validator_indices": [0], "step_validator_indices": [0]}

        class Response:
            def __init__(self, value=None):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        active = {"active_model": "qwen", "mode": "alias", "reasoning_effort": "high",
                  "advisor_model": None, "advisor_reasoning_effort": None,
                  "advisor_sensitivity": "medium", "advisor_trigger": None,
                  "advisor_classifier": None, "fast_mode": False}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                return Response(active if path == "/api/active-model" else {})

        bad_plan_output = json.dumps({"type": "result", "subtype": "success", "result": "{broken"})
        with mock.patch.object(autonomous, "run_validators", return_value=[
                {"argv": ["baseline"], "exit": 0, "stdout": "passed", "stderr": ""}]), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded",
                               return_value=(bad_plan_output, "", 0, None, True)) as planner, \
             mock.patch.object(autonomous, "run_child") as child:
            with self.assertRaisesRegex(ValueError, "rejected after one repair attempt"):
                autonomous.run_request(
                    self.workspace, "Implement a feature", [["acceptance"]],
                    runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                    validator_timeout=5, baseline_validators=[["baseline"]],
                    requirements=[requirement],
                )

        self.assertEqual(planner.call_count, 2)
        child.assert_not_called()
        run_dir = max(self.runs_root.glob("qwen-autonomous-*"), key=lambda item: item.stat().st_mtime)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed_exception")
        self.assertFalse(state["workspace_dirty"])

    def test_run_request_stops_without_continuation_when_required_checkpoint_is_missing(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        (self.workspace / "views").mkdir()
        template = self.workspace / "views" / "page.ejs"
        template.write_text("<p>before</p>\n", encoding="utf-8")
        requirement = {"id": "req-ui", "acceptance": "The EJS page renders correctly",
                       "validator_indices": [0], "step_validator_indices": [0]}
        plan = {"summary": "Update an EJS page", "steps": [{
            "id": "step-ui", "title": "Update EJS", "task": "Update the assigned template and add a render test",
            "files": ["views/page.ejs"], "reference_paths": [], "depends_on": [],
            "checks": ["The page renders"], "covers": ["req-ui"], "step_validator_indices": [0],
            "source_ranges": [{"path": "views/page.ejs", "offset": 1, "limit": 1,
                               "purpose": "Existing template baseline"}],
        }]}
        preverified = {"step-ui": {
            "summary": "The assigned template is one paragraph.",
            "implementation_plan": "Update only the assigned template.",
            "files": [{"path": "views/page.ejs", "symbols": ["paragraph"],
                       "ranges": [{"offset": 1, "limit": 1, "purpose": "Existing template baseline"}]}],
            "risks": [],
        }}
        active = {"active_model": "qwen", "mode": "alias", "reasoning_effort": "high",
                  "advisor_model": None, "advisor_reasoning_effort": None,
                  "advisor_sensitivity": "medium", "advisor_trigger": None,
                  "advisor_classifier": None, "fast_mode": False}

        class Response:
            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                return Response(active if path == "/api/active-model" else {})

        child_calls = []

        def child(*args, **kwargs):
            child_calls.append(kwargs)
            return {"success": False, "exit": 1, "stop_reason": "missing checkpoint",
                    "seconds": 90, "child_tool_calls": ["Read"], "qwen_route_calls": 2,
                    "child_handoff": False, "child_result": "", "final": "",
                    "read_audit": {"read_count": 1, "search_count": 0, "inspection_count": 1,
                                   "counts_by_tool": {"Read": 1, "Grep": 0, "Glob": 0},
                                   "reads": [{"tool": "Read", "path": "views/page.ejs",
                                              "offset": 1, "limit": 1}],
                                   "violations": [], "within_budget": True}}

        passed = [{"argv": ["probe"], "exit": 0, "stdout": "passed", "stderr": ""}]
        before = template.read_text(encoding="utf-8")
        with mock.patch.object(autonomous, "run_validators", return_value=passed), \
                mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
                mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
                mock.patch.object(autonomous.dispatch, "set_policy"), \
                mock.patch.object(autonomous, "run_child", side_effect=child):
            result = autonomous.run_request(
                self.workspace, "Update one EJS template", [["probe"]], runs_root=self.runs_root,
                total_timeout=30, step_timeout=5, validator_timeout=5,
                requirements=[requirement], plan_override=plan,
                preverified_inspections=preverified, checkpoint_validators=[["syntax"]],
            )

        self.assertEqual(result["status"], "stopped_checkpoint_failure")
        self.assertEqual(result["steps"][0]["status"], "failed_checkpoint_missing")
        self.assertEqual(len(child_calls), 1, "a missing checkpoint must not launch a continuation child")
        self.assertEqual(template.read_text(encoding="utf-8"), before)

    def test_preverified_inspection_requires_caller_supplied_plan(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "caller-supplied validated plan"):
            autonomous.run_request(
                self.workspace, "Implement a feature", [["acceptance"]],
                runs_root=self.runs_root,
                requirements=[{"id": "req-feature", "acceptance": "Feature works",
                               "validator_indices": [0], "step_validator_indices": [0]}],
                preverified_inspections={"step-feature": {}})

    def test_run_request_rejects_repeated_read_only_plan_without_worker_dispatch(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        protected_path = "test/test_app.py"
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                       "validator_indices": [0], "step_validator_indices": [0]}
        invalid_plan = {"summary": "Bad target", "steps": [{
            "id": "step-feature", "title": "Feature", "task": "Implement the feature",
            "files": [protected_path], "depends_on": [], "checks": ["Feature passes"],
            "covers": ["req-feature"],
        }]}
        plan_output = json.dumps({"type": "result", "subtype": "success",
                                  "result": json.dumps(invalid_plan)})

        class Response:
            def __init__(self, value=None):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        active = {"active_model": "qwen", "mode": "alias", "reasoning_effort": "high",
                  "advisor_model": None, "advisor_reasoning_effort": None,
                  "advisor_sensitivity": "medium", "advisor_trigger": None,
                  "advisor_classifier": None, "fast_mode": False}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                return Response(active if path == "/api/active-model" else {})

        with mock.patch.object(autonomous, "run_validators", return_value=[
                {"argv": ["baseline"], "exit": 0, "stdout": "passed", "stderr": ""}]), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded",
                               return_value=(plan_output, "", 0, None, True)) as planner, \
             mock.patch.object(autonomous, "run_child") as child:
            with self.assertRaisesRegex(ValueError, "rejected after one repair attempt"):
                autonomous.run_request(
                    self.workspace, "Implement a feature", [["acceptance"]],
                    runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                    validator_timeout=5, baseline_validators=[["baseline"]],
                    requirements=[requirement], read_only_paths=[protected_path],
                )

        self.assertEqual(planner.call_count, 2)
        child.assert_not_called()
        run_dir = max(self.runs_root.glob("qwen-autonomous-*"), key=lambda item: item.stat().st_mtime)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed_exception")
        self.assertFalse(state["workspace_dirty"])
        repair_input = planner.call_args_list[1].kwargs["task_input"]
        self.assertIn("read-only path", repair_input)

    def test_worker_keeps_file_tools_when_coordinator_is_agent_only(self):
        step = {"id": "step-1", "title": "Schema", "task": "Add a schema field",
                "files": ["src/app.py"], "covers": [], "checks": []}
        spec = json.loads(autonomous.step_agent_spec(step, "Request", [], self.workspace, "MARKER", []))
        self.assertEqual(spec[autonomous.AUTONOMOUS_CHILD]["tools"], ["Read", "Grep", "Edit", "Write"])
        inspection = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [], phase="inspection"))
        definition = inspection[autonomous.AUTONOMOUS_CHILD]
        self.assertEqual(definition["tools"], ["Read", "Grep"])
        self.assertIn("read-only checkpoint", definition["prompt"])
        self.assertIn("Grep only on an exact assigned or declared-reference file", definition["prompt"])
        self.assertIn("Small files may be read in full", definition["prompt"])
        self.assertNotIn("Edit", definition["tools"])

    def test_preverified_source_map_removes_redundant_grep_tool(self):
        step = {"id": "step-1", "title": "Update template", "task": "Update the assigned template",
                "files": ["src/app.py"], "covers": [], "checks": []}
        context = {"source": "orchestrator_preverified_source_map", "summary": "source map",
                   "implementation_plan": "edit scoped path", "files": [], "risks": []}
        spec = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [], inspection_context=context))
        definition = spec[autonomous.AUTONOMOUS_CHILD]
        self.assertEqual(definition["tools"], ["Read", "Edit", "Write"])
        self.assertIn("do not use Grep", definition["prompt"])

    def test_ejs_checkpoint_settings_preserve_scope_hook_and_add_batch_hook(self):
        step = {"id": "step-ui", "files": ["views/page.ejs", "src/page.js"],
                "reference_paths": []}
        settings = autonomous.worker_tool_settings(
            self.workspace, step, [], "implementation", self.runs_root / "worker.jsonl",
            checkpoint_validators=[["node", "scripts/check-ejs.js"]])
        payload = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(payload["hooks"]["PreToolUse"][0]["matcher"], "Read|Grep|Edit|Write")
        self.assertEqual(len(payload["hooks"]["PostToolBatch"]), 1)
        hook = payload["hooks"]["PostToolBatch"][0]["hooks"][0]
        self.assertEqual(hook["args"][0], str((Path(__file__).with_name("template_checkpoint.py")).resolve()))
        checkpoint = json.loads((self.runs_root / "worker.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["validators"], [["node", "scripts/check-ejs.js"]])

    def test_ejs_checkpoint_settings_reject_more_than_four_validators(self):
        step = {"id": "step-ui", "files": ["views/page.ejs"], "reference_paths": []}
        with self.assertRaisesRegex(ValueError, "one to four"):
            autonomous.worker_tool_settings(
                self.workspace, step, [], "implementation", self.runs_root / "worker.jsonl",
                checkpoint_validators=[["node", str(index)] for index in range(5)])

    def test_ejs_checkpoint_runs_once_for_successful_scoped_qwen_batch(self):
        template = self.workspace / "views" / "page.ejs"
        template.parent.mkdir()
        template.write_text("<p>ok</p>\n", encoding="utf-8")
        receipt = self.runs_root / "receipt.json"
        scope = {"root": str(self.workspace), "write_allowed": ["views/page.ejs"],
                 "validators": [["node", "scripts/check-ejs.js"]]}
        payload = {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
            "tool_name": "Edit", "tool_input": {"file_path": str(template)},
            "tool_response": "The file has been updated successfully.",
        }]}
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="EJS syntax valid", stderr="")
        with mock.patch.object(template_checkpoint.subprocess, "run", return_value=completed) as validator, \
                mock.patch("builtins.print") as emit:
            result = template_checkpoint.run_checkpoint(payload, scope, receipt)
            again = template_checkpoint.run_checkpoint(payload, scope, receipt)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["paths"], ["views/page.ejs"])
        self.assertIsNone(again)
        validator.assert_called_once()
        self.assertGreater(validator.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(validator.call_args.kwargs["timeout"], 25)
        self.assertIn("passed", emit.call_args.args[0])

    def test_ejs_checkpoint_ignores_reads_js_out_of_scope_failed_and_coordinator_calls(self):
        scope = {"root": str(self.workspace), "write_allowed": ["views/page.ejs"],
                 "validators": [["node", "check.js"]]}
        cases = [
            {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
                "tool_name": "Read", "tool_input": {"file_path": "views/page.ejs"}, "tool_response": {}}]},
            {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
                "tool_name": "Edit", "tool_input": {"file_path": "src/page.js"}, "tool_response": {}}]},
            {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
                "tool_name": "Edit", "tool_input": {"file_path": "views/page.ejs"},
                "tool_response": "Error: old_string not found in file"}]},
            {"agent_type": "coordinator", "tool_calls": [{
                "tool_name": "Write", "tool_input": {"file_path": "views/page.ejs"}, "tool_response": {}}]},
        ]
        with mock.patch.object(template_checkpoint.subprocess, "run") as validator:
            for index, payload in enumerate(cases):
                self.assertIsNone(template_checkpoint.run_checkpoint(
                    payload, scope, self.runs_root / f"receipt-{index}.json"))
        validator.assert_not_called()

    def test_ejs_checkpoint_recognizes_serialized_tool_result_errors(self):
        scope = {"root": str(self.workspace), "write_allowed": ["views/page.ejs"],
                 "validators": [["node", "check.js"]]}
        for response in ("Error: write failed", [{"type": "text", "text": "Permission denied"}],
                         {"is_error": True, "text": "failed"}):
            payload = {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
                "tool_name": "Write", "tool_input": {"file_path": "views/page.ejs"},
                "tool_response": response,
            }]}
            with self.subTest(response=response), mock.patch.object(template_checkpoint.subprocess, "run") as validator:
                self.assertIsNone(template_checkpoint.run_checkpoint(
                    payload, scope, self.runs_root / f"error-{hash(str(response))}.json"))
                validator.assert_not_called()

    def test_ejs_checkpoint_returns_bounded_failure_diagnostics(self):
        template = self.workspace / "bad.ejs"
        template.write_text("<% bad %>", encoding="utf-8")
        scope = {"root": str(self.workspace), "write_allowed": ["bad.ejs"],
                 "validators": [["node", "check.js"]]}
        payload = {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
            "tool_name": "Write", "tool_input": {"file_path": "bad.ejs"},
            "tool_response": {"success": True},
        }]}
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="syntax error", stderr="line 1")
        with mock.patch.object(template_checkpoint.subprocess, "run", return_value=failed), \
                mock.patch("builtins.print") as emit:
            result = template_checkpoint.run_checkpoint(payload, scope, self.runs_root / "failed.json")
        self.assertEqual(result["status"], "failed")
        self.assertIn("syntax error", emit.call_args.args[0])
        self.assertLessEqual(len(json.loads(emit.call_args.args[0])["hookSpecificOutput"]["additionalContext"]),
                             template_checkpoint.MAX_CONTEXT_CHARS)

    def test_live_transcript_flushes_and_redacts_while_process_is_running(self):
        transcript = self.runs_root / "worker.jsonl"
        source = "import time; print('Bearer secret-value', flush=True); time.sleep(1.2)"
        result = {}

        def invoke():
            result["value"] = dispatch.run_cli_bounded(
                [sys.executable, "-c", source], task_input="", cwd=self.workspace,
                env=dict(os.environ), timeout=5, idle_timeout=5,
                transcript_path=transcript, redact_values=("secret-value",),
            )

        thread = threading.Thread(target=invoke)
        thread.start()
        deadline = time.monotonic() + 3
        seen_live = False
        while time.monotonic() < deadline and thread.is_alive():
            if transcript.exists() and transcript.stat().st_size:
                seen_live = True
                break
            time.sleep(0.025)
        thread.join(timeout=5)
        self.assertTrue(seen_live, "transcript was not flushed until after CLI exit")
        self.assertFalse(thread.is_alive())
        saved = transcript.read_text(encoding="utf-8")
        self.assertNotIn("secret-value", saved)
        self.assertIn("Bearer [REDACTED]", saved)
        self.assertEqual(result["value"][2], 0)

    def test_missing_ejs_checkpoint_stops_worker_at_its_own_deadline(self):
        missing_receipt = self.runs_root / "missing-checkpoint.json"
        result = dispatch.run_cli_bounded(
            [sys.executable, "-c", "import time; time.sleep(3)"], task_input="",
            cwd=self.workspace, env=dict(os.environ), timeout=10, idle_timeout=10,
            checkpoint_receipt_path=missing_receipt, checkpoint_deadline=1,
        )
        self.assertEqual(result[3], "Qwen EJS checkpoint was not reached within 1 seconds")
        self.assertTrue(result[4])

    def test_repair_without_ejs_checkpoint_remains_under_normal_total_timeout(self):
        receipt = self.runs_root / "repair-checkpoint.json"
        result = dispatch.run_cli_bounded(
            [sys.executable, "-c", "import time; time.sleep(3)"], task_input="",
            cwd=self.workspace, env=dict(os.environ), timeout=1, idle_timeout=2,
            checkpoint_receipt_path=receipt, checkpoint_deadline=None,
        )
        self.assertEqual(result[3], "Claude Code exceeded the 1-second total task limit")
        self.assertTrue(result[4])

    def test_incomplete_checkpoint_receipt_does_not_satisfy_watchdog(self):
        receipt = self.runs_root / "receipt.json"
        receipt.write_text('{"status":"running"}', encoding="utf-8")
        self.assertFalse(dispatch.checkpoint_receipt_complete(receipt))
        receipt.write_text('{"status":"passed"}', encoding="utf-8")
        self.assertTrue(dispatch.checkpoint_receipt_complete(receipt))

    def test_validators_share_total_deadline_and_stop_when_exhausted(self):
        calls = []

        def record(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return subprocess.CompletedProcess(args[0], 0, "", "")

        import subprocess
        deadline = time.monotonic() + 2
        with mock.patch.object(autonomous.subprocess, "run", side_effect=record):
            results = autonomous.run_validators(self.workspace, [["one"], ["two"]],
                                                timeout=10, deadline=deadline)
        self.assertEqual([item["exit"] for item in results], [0, 0])
        self.assertTrue(all(0 < value <= 2 for value in calls))

    def test_reconciliation_uses_running_repair_attempt_on_accepted_step(self):
        state_path = self.workspace / "state.json"
        before = autonomous.file_hashes(self.workspace)
        state = {"workspace": str(self.workspace.resolve()), "status": "executing",
                 "initial_hashes": before, "steps": [{"status": "accepted", "attempts": [
                     {"status": "accepted", "before_hashes": before},
                     {"status": "running", "before_hashes": before,
                      "kind": "final_acceptance_repair"},
                 ]}]}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (self.workspace / "src" / "app.py").write_text("partial repair\n", encoding="utf-8")
        result = autonomous.reconcile_interrupted_state(state_path, self.workspace)
        self.assertEqual(result["status"], "interrupted_dirty")
        self.assertFalse(result["resume_allowed"])


if __name__ == "__main__":
    unittest.main()
