"""Contract tests for generic Qwen plan/execute/verify dispatch."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest import mock

import autonomous
import run as dispatch
import tool_scope_guard
import tool_result_guard
import template_checkpoint

ROUTE_LOGS = (
    "completion() model= qwen3.8-27b\n"
    "sift_request protocol=anthropic_messages compacted=false active_tool_results_compacted=0 "
    "input_tokens=100 forwarded_tokens=100 target=32768 output_limit=4096\n"
)


def write_test_result_cap_receipts(transcript_path: Path, events: list[dict]) -> None:
    """Emulate the PostToolUse hook for CLI event fixtures with small outputs."""
    settings = json.loads(transcript_path.with_suffix(".settings.json").read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {}).get("PostToolUse", [])
    if not hooks:
        return
    scope_path = Path(hooks[0]["hooks"][0]["args"][1])
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    audit_path = Path(scope["result_audit_path"])
    tool_ids = {block.get("id"): block.get("name") for event in events if event.get("type") == "assistant"
                for block in autonomous.message_blocks(event)
                if block.get("type") == "tool_use" and block.get("name") in {"Read", "Grep"}}
    with audit_path.open("a", encoding="utf-8") as audit_file:
        for event in events:
            if event.get("type") != "user":
                continue
            for block in autonomous.message_blocks(event):
                if block.get("type") != "tool_result" or block.get("tool_use_id") not in tool_ids:
                    continue
                size = len(json.dumps(block.get("content", ""), ensure_ascii=False,
                                      separators=(",", ":")))
                audit_file.write(json.dumps({"tool_use_id": block["tool_use_id"],
                                             "tool": tool_ids[block["tool_use_id"]], "changed": False,
                                             "original_chars": size, "delivered_chars": size}) + "\n")


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
        normalized = autonomous.validate_plan(plan, self.workspace, max_files_per_step=3)
        self.assertEqual(len(normalized["steps"]), 2)
        self.assertEqual(len(normalized["steps"][0]["files"]), 3)

    def test_plan_adds_exact_local_js_import_as_read_only_reference(self):
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "routes.js").write_text(
            "const domain = require('./domain');\nmodule.exports = domain;\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-route", "title": "Update route", "task": "Update route",
                           "files": ["src/routes.js"], "depends_on": [], "checks": []}]}
        result = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(result["steps"][0]["reference_paths"], ["src/domain.js"])

    def test_rejects_change_target_misclassified_as_read_only_reference(self):
        (self.workspace / "src" / "api.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "client.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "server.js").write_text("module.exports = {};\n", encoding="utf-8")
        plan = {"steps": [{
            "id": "step-1", "title": "Wire rescheduling",
            "task": "Update src/server.js to call the new client helper.",
            "files": ["src/api.js", "src/client.js"],
            "reference_paths": ["src/server.js"], "depends_on": [], "checks": [],
        }]}
        with self.assertRaisesRegex(ValueError, "change target"):
            autonomous.validate_plan(plan, self.workspace, max_files_per_step=2)

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

    def test_allows_test_only_step_only_when_caller_explicitly_authorizes_it(self):
        plan = {"steps": [{"id": "step-test-only", "title": "Add regression test",
                           "task": "Add the requested regression coverage",
                           "files": ["test/new-feature.test.cjs"], "depends_on": [], "checks": []}]}
        validated = autonomous.validate_plan(
            plan, self.workspace, test_only_paths=["test/new-feature.test.cjs"])
        self.assertEqual(validated["steps"][0]["files"], ["test/new-feature.test.cjs"])
        with self.assertRaisesRegex(ValueError, "test-only path allowlist"):
            autonomous.validate_plan(
                plan, self.workspace, test_only_paths=["test/another-test.test.cjs"])

    def test_test_only_change_requires_non_test_progress_or_exact_caller_allowlist(self):
        self.assertFalse(autonomous.step_changes_match_mode(["test/test_app.py"], []))
        self.assertTrue(autonomous.step_changes_match_mode(
            ["test/test_app.py"], ["test/test_app.py"]))
        self.assertFalse(autonomous.step_changes_match_mode(
            ["test/test_app.py"], ["test/other_test.py"]))
        self.assertTrue(autonomous.step_changes_match_mode(
            ["src/app.py", "test/test_app.py"], []))

    def test_recognizes_common_test_filename_conventions(self):
        for path in ("test_appointment.py", "testbooking.py", "appointment_test.py",
                     "booking_spec.rb",
                     "booking_test.go", "AppointmentTest.java", "appointment.spec.ts",
                     "testAppointment.js", "spec/ordinary-name.rb"):
            with self.subTest(path=path):
                self.assertTrue(autonomous.is_test_path(path))
        for path in ("src/latest-report.js", "src/test-data.js"):
            with self.subTest(path=path):
                self.assertFalse(autonomous.is_test_path(path))

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
        plan["steps"][0]["reference_paths"] = ["test/test_app.py", "test/test_app.py"]
        with self.assertRaisesRegex(ValueError, "unique and read-only"):
            autonomous.validate_plan(plan, self.workspace)
        plan["steps"][0]["reference_paths"] = ["src/missing.js"]
        with self.assertRaisesRegex(ValueError, "references missing files"):
            autonomous.validate_plan(plan, self.workspace)

    def test_plan_allows_negative_change_verb_near_read_only_reference(self):
        plan = {"steps": [{"id": "step-ref", "title": "Update app", "task":
                           "Edit only src/app.py. Read test/test_app.py to understand the assertion, "
                           "but do not modify it.",
                           "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                           "depends_on": [], "checks": [
                               "Update src/app.py; keep test/test_app.py unchanged."], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["test/test_app.py"])

    def test_plan_allows_explicit_do_not_edit_reference_in_positive_task_clause(self):
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "other.js").write_text("module.exports = {};\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-ref", "title": "Add owner controls",
                           "task": ("Add the owner-facing route using src/domain.js as a read-only reference "
                                    "(do not edit src/other.js or src/domain.js)."),
                           "files": ["src/app.py"], "reference_paths": ["src/domain.js", "src/other.js"],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["src/domain.js", "src/other.js"])

    def test_plan_allows_read_only_acceptance_test_named_as_validation_target(self):
        (self.workspace / "test" / "test_app.py").write_text("assert True\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-implementation", "title": "Update app",
                           "task": "Update src/app.py and make test/test_app.py pass without editing it.",
                           "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(
            plan, self.workspace, read_only_paths=["test/test_app.py"])
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["test/test_app.py"])

    def test_plan_rejects_indirect_edit_target_of_read_only_reference(self):
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        for task in (
            "Read src/domain.js. Update it to expose the new option.",
            "Update the helper. Its file is src/domain.js.",
            "Update the eligibility helper. Its file is src/domain.js.",
            "Update the helper logic. Its file is src/domain.js.",
            "Update the eligibility API. Its source file is src/domain.js.",
        ):
            with self.subTest(task=task):
                plan = {"steps": [{"id": "step-ref", "title": "Update route", "task": task,
                                   "files": ["src/app.py"], "reference_paths": ["src/domain.js"],
                                   "depends_on": [], "checks": [], "covers": []}]}
                with self.assertRaisesRegex(ValueError, "change target"):
                    autonomous.validate_plan(plan, self.workspace)

    def test_plan_allows_read_only_reference_explicitly_reused_by_writable_page(self):
        (self.workspace / "views").mkdir(exist_ok=True)
        (self.workspace / "views" / "page.ejs").write_text("<main></main>\n", encoding="utf-8")
        (self.workspace / "views" / "partial.ejs").write_text("<div></div>\n", encoding="utf-8")
        plan = {"steps": [{
            "id": "step-page", "title": "Update the booking page",
            "task": "Update views/page.ejs (reusing views/partial.ejs and existing markup conventions).",
            "files": ["views/page.ejs"], "reference_paths": ["views/partial.ejs"],
            "depends_on": [], "checks": [], "covers": [],
        }]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["views/partial.ejs"])

    def test_plan_allows_read_only_reference_named_as_inspection_excerpt(self):
        (self.workspace / "test" / "test_app.py").write_text("assert True\\n", encoding="utf-8")
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\\n", encoding="utf-8")
        plan = {"steps": [{
            "id": "step-test", "title": "Add exact duration acceptance",
            "task": ("Add a focused assertion to test/test_app.py after inspecting the relevant excerpts of "
                     "src/domain.js (availability API and returned slot fields)."),
            "files": ["test/test_app.py"], "reference_paths": ["src/domain.js"],
            "depends_on": [], "checks": [], "covers": [],
        }]}
        normalized = autonomous.validate_plan(plan, self.workspace, test_only_paths=["test/test_app.py"])
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["src/domain.js"])

    def test_plan_allows_implementation_per_read_only_acceptance_contract(self):
        (self.workspace / "src" / "app.py").write_text("def app(): pass\n", encoding="utf-8")
        (self.workspace / "test" / "booking-addons.acceptance.md").parent.mkdir(exist_ok=True)
        (self.workspace / "test" / "booking-addons.acceptance.md").write_text(
            "# Read-only contract\n", encoding="utf-8")
        plan = {"steps": [{
            "id": "step-domain", "title": "Implement domain rules",
            "task": "Implement domain behavior per the immutable test/booking-addons.acceptance.md contract.",
            "files": ["src/app.py"],
            "reference_paths": ["test/booking-addons.acceptance.md"],
            "depends_on": [], "checks": [], "covers": [],
        }]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"],
                         ["test/booking-addons.acceptance.md"])

    def test_plan_still_rejects_read_only_reference_explicitly_named_as_edit_target(self):
        (self.workspace / "views").mkdir(exist_ok=True)
        (self.workspace / "views" / "page.ejs").write_text("<main></main>\n", encoding="utf-8")
        (self.workspace / "views" / "partial.ejs").write_text("<div></div>\n", encoding="utf-8")
        plan = {"steps": [{
            "id": "step-page", "title": "Update the booking page",
            "task": "Update views/page.ejs using views/partial.ejs, then modify views/partial.ejs.",
            "files": ["views/page.ejs"], "reference_paths": ["views/partial.ejs"],
            "depends_on": [], "checks": [], "covers": [],
        }]}
        with self.assertRaisesRegex(ValueError, "change target"):
            autonomous.validate_plan(plan, self.workspace)

    def test_plan_allows_unrelated_adjacent_edit_and_read_only_reference(self):
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-ref", "title": "Update route", "task":
                           "Update the route handler. Read src/domain.js for its interface, "
                           "but do not modify it.",
                           "files": ["src/app.py"], "reference_paths": ["src/domain.js"],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["src/domain.js"])

    def test_plan_allows_read_only_reference_far_from_an_unrelated_change_verb(self):
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "schema.js").write_text("module.exports = {};\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-domain", "title": "Implement domain behavior", "task":
                           ("Inspect src/schema.js and the existing tests, then implement the booking "
                            "domain rules in src/domain.js while preserving the accepted schema."),
                           "files": ["src/domain.js"], "reference_paths": ["src/schema.js"],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["src/schema.js"])

    def test_plan_allows_schema_inspection_before_domain_implementation(self):
        (self.workspace / "src" / "booking-domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "service-extra-eligibility.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "appointment-schema.js").write_text("module.exports = {};\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-1", "title": "Domain validation", "task":
                           ("Inspect src/booking-domain.js, src/service-extra-eligibility.js, "
                            "src/appointment-schema.js, and the read-only domain/integrity tests, "
                            "then implement server-authoritative service_extras handling in the booking domain."),
                           "files": ["src/booking-domain.js", "src/service-extra-eligibility.js"],
                           "reference_paths": ["src/appointment-schema.js"], "depends_on": [],
                           "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], ["src/appointment-schema.js"])

    def test_inferred_import_already_assigned_is_not_added_as_read_only_reference(self):
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.workspace / "src" / "routes.js").write_text(
            "const domain = require('./domain');\nmodule.exports = domain;\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-route", "title": "Update route", "task": "Update route",
                           "files": ["src/routes.js", "src/domain.js"], "depends_on": [], "checks": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["reference_paths"], [])

    def test_test_only_plan_does_not_infer_production_imports(self):
        test_file = self.workspace / "test" / "booking.test.cjs"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("const domain = require('../src/domain');\n", encoding="utf-8")
        (self.workspace / "src" / "domain.js").write_text("module.exports = {};\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-test", "title": "Add holdout", "task": "Add holdout",
                           "files": ["test/booking.test.cjs"], "depends_on": [], "checks": []}]}
        normalized = autonomous.validate_plan(
            plan, self.workspace, test_only_paths=["test/booking.test.cjs"])
        self.assertEqual(normalized["steps"][0]["reference_paths"], [])

    def test_plan_validates_and_preserves_bounded_source_landmarks(self):
        (self.workspace / "src" / "app.py").write_text("line one\nline two\nline three\n", encoding="utf-8")
        plan = {"steps": [{"id": "step-source-map", "title": "Feature", "task": "Implement",
                           "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                           "source_ranges": [{"path": "src/app.py", "offset": 2, "limit": 4,
                                              "purpose": "Existing handler"}],
                           "depends_on": [], "checks": [], "covers": []}]}
        normalized = autonomous.validate_plan(plan, self.workspace)
        self.assertEqual(normalized["steps"][0]["source_ranges"], [
            {"path": "src/app.py", "offset": 2, "limit": 2, "purpose": "Existing handler"}])
        self.assertEqual(plan["steps"][0]["source_ranges"][0]["limit"], 4,
                         "normalization must not mutate the model's source plan")
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
        self.assertIn("Caller-verified source ranges (inspection reads are limited", prompt)
        self.assertIn("src/app.py lines 2-5: Current route validation", prompt)

    def test_source_scout_is_bounded_and_read_only(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 450, encoding="utf-8")
        step = {"id": "step-large", "title": "Inspect large source", "task": "Find the route",
                "files": ["src/large.js"], "reference_paths": ["test/test_app.py"],
                "source_ranges": [], "covers": [], "checks": []}
        spec = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [], phase="scout",
            inspection_limits=autonomous.SOURCE_SCOUT_LIMITS))[autonomous.AUTONOMOUS_CHILD]
        self.assertEqual(spec["tools"], ["Read", "Grep"])
        self.assertEqual(spec["maxTurns"], 24)
        self.assertIn("summary\": string (<=4000 chars)", spec["prompt"])
        self.assertIn("head_limit to 100 or less", spec["prompt"])
        self.assertIn("420 seconds", spec["prompt"])
        self.assertIn("at most 13 permitted Read/Grep operations", spec["prompt"])
        self.assertIn("no more than 5 Reads and 7 Greps", spec["prompt"])
        self.assertIn("A maximum of 4 guard-denied attempts is tolerated", spec["prompt"])
        self.assertIn("denied calls do not access files", spec["prompt"])
        self.assertIn("Do not delegate", spec["prompt"])
        inspection_spec = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [], phase="inspection",
            inspection_limits=autonomous.SOURCE_SCOUT_LIMITS))[autonomous.AUTONOMOUS_CHILD]
        self.assertIn("at most 12000 characters", inspection_spec["prompt"])
        self.assertIn("summary <=4000 chars", inspection_spec["prompt"])
        self.assertTrue(autonomous.needs_source_scout(self.workspace, step))
        step["source_ranges"] = [{"path": "src/large.js", "offset": 210, "limit": 80,
                                  "purpose": "caller hint"}]
        self.assertFalse(autonomous.needs_source_scout(self.workspace, step))
        step["source_ranges"] = []
        transcript = self.runs_root / "step-large-scout.jsonl"
        autonomous.worker_tool_settings(self.workspace, step, ["test/test_app.py"], "scout", transcript)
        scope = json.loads(transcript.with_suffix(".scope.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["write_allowed"], [])
        self.assertEqual(scope["read_allowed"], ["src/large.js", "test/test_app.py"])
        self.assertEqual(scope["scout_tool_call_limit"], 13)
        self.assertEqual(scope["scout_tool_limits"], {"Read": 5, "Grep": 7, "Glob": 7})
        self.assertEqual(scope["scout_denied_attempt_limit"], 4)
        self.assertEqual(scope["range_restricted"], [])

    def test_source_scout_grep_requires_bounded_exact_file_search(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 450, encoding="utf-8")
        scope = {"root": str(self.workspace), "read_allowed": ["src/large.js"],
                 "write_allowed": [], "scout_mode": True, "range_restricted": []}
        path = str(large.resolve())
        self.assertIsNone(tool_scope_guard.authorize(
            "Grep", {"path": path, "pattern": "route", "head_limit": 10, "-A": 5}, scope))
        self.assertIsNone(tool_scope_guard.authorize(
            "Grep", {"path": path, "pattern": "route", "head_limit": 100}, scope))
        self.assertIn("head_limit", tool_scope_guard.authorize(
            "Grep", {"path": path, "pattern": "route"}, scope))
        self.assertIn("context", tool_scope_guard.authorize(
            "Grep", {"path": path, "pattern": "route", "head_limit": 10, "-A": 6}, scope))
        self.assertIn("head_limit", tool_scope_guard.authorize(
            "Grep", {"path": path, "pattern": "route", "head_limit": 101}, scope))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Grep", {"path": str(self.workspace / "src"), "pattern": "route", "head_limit": 20}, scope))
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": path, "offset": 300, "limit": 80}, scope))
        self.assertIn("capped at 80", tool_scope_guard.authorize(
            "Read", {"file_path": path, "offset": 1, "limit": 81}, scope))
        self.assertIn("offset and limit", tool_scope_guard.authorize(
            "Read", {"file_path": path, "offset": 1, "limit": 300}, scope))

    def test_source_scout_pretool_hook_separates_permitted_and_denied_budgets(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 450, encoding="utf-8")
        counter_dir = self.runs_root / "scout-calls"
        counter_dir.mkdir()
        scope_path = self.runs_root / "scout.scope.json"
        scope_path.write_text(json.dumps({
            "root": str(self.workspace), "read_allowed": ["src/large.js"], "write_allowed": [],
            "scout_mode": True, "scout_call_counter_dir": str(counter_dir),
            "scout_tool_call_limit": 20,
            "scout_tool_limits": {"Read": 4, "Grep": 3, "Glob": 3},
            "range_restricted": [],
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")), str(scope_path)]
        read_payload = {"tool_name": "Read", "tool_input": {
            "file_path": str(large.resolve()), "offset": 1, "limit": 80,
        }}
        grep_payload = {"tool_name": "Grep", "tool_input": {
            "path": str(large.resolve()), "pattern": "route", "head_limit": 20,
        }}
        def invoke(payload):
            return subprocess.run(hook, input=json.dumps(payload), text=True, capture_output=True, check=True)
        for payload, limit, expected_reason in ((read_payload, 4, "read attempts"),
                                                 (grep_payload, 3, "search attempts")):
            with ThreadPoolExecutor(max_workers=limit + 2) as pool:
                results = list(pool.map(lambda _: invoke(payload), range(limit + 2)))
            allowed = [result for result in results if not result.stdout.strip()]
            denied = [result for result in results if result.stdout.strip()]
            self.assertEqual(len(allowed), limit)
            self.assertEqual(len(denied), 2)
            self.assertTrue(all(expected_reason in json.loads(result.stdout)["hookSpecificOutput"][
                "permissionDecisionReason"] for result in denied))
        self.assertEqual(len(list(counter_dir.glob("total-*.ticket"))), 7)
        self.assertEqual(len(list(counter_dir.glob("denied-*.ticket"))), 4)

    def test_denied_scout_call_does_not_consume_permitted_operation_budget(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 450, encoding="utf-8")
        counter_dir = self.runs_root / "scout-denied-call"
        counter_dir.mkdir()
        scope_path = self.runs_root / "scout-denied.scope.json"
        scope_path.write_text(json.dumps({
            "root": str(self.workspace), "read_allowed": ["src/large.js"], "write_allowed": [],
            "scout_mode": True, "scout_call_counter_dir": str(counter_dir),
            "scout_tool_call_limit": 1, "scout_tool_limits": {"Read": 4, "Grep": 3},
            "scout_denied_attempt_limit": 2,
            "range_restricted": [],
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")), str(scope_path)]
        denied = subprocess.run(hook, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": str(large.resolve()), "offset": 1, "limit": 81},
        }), text=True, capture_output=True, check=True)
        allowed = subprocess.run(hook, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": str(large.resolve()), "offset": 1, "limit": 80},
        }), text=True, capture_output=True, check=True)
        over_quota = subprocess.run(hook, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": str(large.resolve()), "offset": 2, "limit": 80},
        }), text=True, capture_output=True, check=True)
        self.assertIn("capped at 80", denied.stdout)
        self.assertEqual(allowed.stdout, "")
        self.assertIn("combined Read/Grep attempts", over_quota.stdout)
        self.assertEqual(len(list(counter_dir.glob("total-*.ticket"))), 1)
        self.assertEqual(len(list(counter_dir.glob("read-*.ticket"))), 1)
        self.assertEqual(len(list(counter_dir.glob("denied-*.ticket"))), 2)

    def test_scout_audit_counts_only_guard_approved_source_operations(self):
        path = self.workspace / "src" / "app.py"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "denied", "name": "Read", "input": {
                    "file_path": str(path), "offset": 1, "limit": 400}},
                {"type": "tool_use", "id": "allowed", "name": "Read", "input": {
                    "file_path": str(path), "offset": 1, "limit": 1}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "denied", "is_error": True,
                 "content": "QWEN_SCOPE_GUARD_DENY: Read limit is capped"},
                {"type": "tool_result", "tool_use_id": "allowed", "is_error": False, "content": "value = 1"},
            ]}},
        ]
        audit = autonomous.audit_worker_reads(
            self.workspace, events, allowed_paths=["src/app.py"], require_ranged_reads=True,
            limits={"reads": 1, "searches": 0, "inspections": 1})
        attempts = autonomous.scout_tool_attempt_counts(events)
        self.assertEqual(attempts, {"Read": 1, "Grep": 0})
        self.assertEqual(audit["read_count"], 1)
        self.assertEqual(audit["inspection_count"], 1)
        self.assertTrue(audit["reads"][0]["denied_before_execution"])
        self.assertFalse(audit["reads"][1]["denied_before_execution"])
        self.assertTrue(audit["within_budget"])

    def test_denied_read_of_not_yet_created_assigned_path_does_not_fail_audit(self):
        missing = self.workspace / "src" / "new-route.js"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "denied", "name": "Read", "input": {
                    "file_path": str(missing), "offset": 1, "limit": 200}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "denied", "is_error": True,
                 "content": "QWEN_SCOPE_GUARD_DENY: target is assigned for writing but does not exist yet"},
            ]}},
        ]
        audit = autonomous.audit_worker_reads(
            self.workspace, events, allowed_paths=["src/new-route.js"], require_ranged_reads=True)
        self.assertEqual(audit["read_count"], 0)
        self.assertTrue(audit["within_budget"])
        self.assertEqual(audit["violations"], [])
        self.assertEqual(len(audit["reads"]), 1)
        self.assertTrue(audit["reads"][0]["denied_before_execution"])

    def test_inspection_handoff_accepts_one_json_fence_with_preface(self):
        source = self.workspace / "src" / "fenced.js"
        source.write_text("router.get('/x', handler);\n", encoding="utf-8")
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": "src/fenced.js", "symbols": ["handler"],
                             "ranges": [{"offset": 1, "limit": 1, "purpose": "route"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/fenced.js", "offset": 1, "limit": 1}]}
        handoff = ("Here is the requested source map.\n```json\n" + json.dumps(report)
                   + "\n```\nQWEN_AUTONOMOUS_COMPLETE_test")
        validated = autonomous.validate_inspection_handoff(
            handoff, self.workspace, ["src/fenced.js"], audit)
        self.assertEqual(validated["files"][0]["path"], "src/fenced.js")

    def test_inspection_handoff_rejects_multiple_json_fences(self):
        text = "```json\n{}\n```\n```json\n{}\n```\nQWEN_AUTONOMOUS_COMPLETE_test"
        with self.assertRaisesRegex(ValueError, "exactly one JSON object"):
            autonomous.validate_inspection_handoff(text, self.workspace, [], {"reads": []})

    def test_inspection_handoff_accepts_one_json_object_after_preface(self):
        source = self.workspace / "src" / "prefaced.js"
        source.write_text("module.exports = {};\n", encoding="utf-8")
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": "src/prefaced.js", "symbols": ["handler"],
                             "ranges": [{"offset": 1, "limit": 1, "purpose": "route"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/prefaced.js", "offset": 1, "limit": 1}]}
        handoff = "All scoped reads are complete.\n\n" + json.dumps(report) + "\nQWEN_AUTONOMOUS_COMPLETE_test"
        validated = autonomous.validate_inspection_handoff(
            handoff, self.workspace, ["src/prefaced.js"], audit)
        self.assertEqual(validated["files"][0]["path"], "src/prefaced.js")

    def test_inspection_handoff_clamps_ranges_to_lines_returned_at_eof(self):
        source = self.workspace / "src" / "short.js"
        source.write_text("one\ntwo\nthree\n", encoding="utf-8")
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": "src/short.js", "symbols": ["handler"],
                             "ranges": [{"offset": 1, "limit": 80, "purpose": "short module"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/short.js", "offset": 1, "limit": 80}]}
        validated = autonomous.validate_inspection_handoff(
            json.dumps(report), self.workspace, ["src/short.js"], audit)
        self.assertEqual(validated["files"][0]["ranges"][0]["limit"], 3)

    def test_scout_budget_separates_permitted_calls_denials_and_missing_results(self):
        within = autonomous.source_scout_budget_violations(
            {"Read": 3, "Grep": 6}, {"Read": 4, "Grep": 8})
        self.assertEqual(within, [], "six permitted Greps plus two denied Greps stay within limits")
        over_success = autonomous.source_scout_budget_violations(
            {"Read": 3, "Grep": 8}, {"Read": 3, "Grep": 9})
        self.assertTrue(any(item["reason"] == "source_scout_per_tool_attempt_budget_exceeded"
                            for item in over_success))
        over_denied = autonomous.source_scout_budget_violations(
            {"Read": 1, "Grep": 1}, {"Read": 4, "Grep": 3})
        self.assertEqual(over_denied[0]["reason"], "source_scout_denied_attempt_budget_exceeded")
        missing_result = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "unfinished", "name": "Read", "input": {}}]}}]
        self.assertEqual(autonomous.scout_tool_attempt_counts(missing_result),
                         {"Read": 1, "Grep": 0}, "missing results count as permitted to fail closed")

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
        # Claude Read defaults an omitted offset to line1; a bounded limit is sufficient.
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": str(large), "limit": 100}, scope))
        for invalid in ({"limit": 201}, {"limit": True}, {"limit": 0},
                        {"offset": None, "limit": 100}, {"offset": 0, "limit": 100}):
            self.assertIsNotNone(tool_scope_guard.authorize(
                "Read", {"file_path": str(large), **invalid}, scope))
        restricted = {**scope, "range_restricted": [str(large.resolve())],
                      "read_ranges": {str(large.resolve()): [{"offset": 1, "limit": 200}]}}
        self.assertIsNotNone(tool_scope_guard.authorize(
            "Read", {"file_path": str(large), "limit": 100}, restricted))
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": str(large), "offset": 1, "limit": 100}, restricted))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Grep", {"path": str(self.workspace / "src")}, scope))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Read", {"file_path": str(self.workspace / "test/test_app.py")}, scope))
        self.assertIn("exact assigned", tool_scope_guard.authorize(
            "Edit", {"file_path": str(self.workspace / "test/test_app.py")}, scope))

    def test_inspection_hook_limits_reads_to_caller_ranges_and_denies_grep(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 800, encoding="utf-8")
        step = {"id": "step-test", "title": "Bounded inspection", "files": ["src/large.js"],
                "reference_paths": [], "source_ranges": [
                    {"path": "src/large.js", "offset": 251, "limit": 125, "purpose": "schema"},
                    {"path": "src/large.js", "offset": 610, "limit": 95, "purpose": "transaction"},
                ]}
        transcript = self.runs_root / "step-test-inspection.jsonl"
        autonomous.worker_tool_settings(self.workspace, step, [], "inspection", transcript)
        scope_path = transcript.with_suffix(".scope.json")
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        path = str(large.resolve())
        self.assertEqual(scope["read_ranges"][path], [
            {"offset": 251, "limit": 125}, {"offset": 610, "limit": 95},
        ])
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": path, "offset": 260, "limit": 50}, scope))
        self.assertIsNotNone(tool_scope_guard.authorize(
            "Read", {"file_path": path, "offset": 1, "limit": 200}, scope))
        self.assertIsNotNone(tool_scope_guard.authorize(
            "Read", {"file_path": path, "offset": 360, "limit": 200}, scope))
        self.assertIsNotNone(tool_scope_guard.authorize(
            "Grep", {"path": path, "pattern": "line"}, scope))
        hook = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")),
                str(scope_path)]
        allowed = subprocess.run(hook, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": path, "offset": 260, "limit": 50},
        }), text=True, capture_output=True, check=True)
        denied = subprocess.run(hook, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": path, "offset": 1, "limit": 200},
        }), text=True, capture_output=True, check=True)
        grep_denied = subprocess.run(hook, input=json.dumps({
            "tool_name": "Grep", "tool_input": {"path": path, "pattern": "line"},
        }), text=True, capture_output=True, check=True)
        self.assertEqual(allowed.stdout, "")
        self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(json.loads(grep_denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_implementation_can_read_new_ranges_in_assigned_files_but_not_references(self):
        large = self.workspace / "src" / "large.js"
        reference = self.workspace / "src" / "reference.js"
        large.write_text("line\n" * 800, encoding="utf-8")
        reference.write_text("ref\n" * 800, encoding="utf-8")
        step = {"id": "step-test", "title": "Implement from verified source map",
                "files": ["src/large.js"], "reference_paths": ["src/reference.js"]}
        context = {"files": [
            {"path": "src/large.js", "ranges": [{"offset": 251, "limit": 100}]},
            {"path": "src/reference.js", "ranges": [{"offset": 40, "limit": 30}]},
        ]}
        transcript = self.runs_root / "step-test-implementation.jsonl"
        autonomous.worker_tool_settings(
            self.workspace, step, [], "implementation", transcript, inspection_context=context)
        scope = json.loads(transcript.with_suffix(".scope.json").read_text(encoding="utf-8"))
        assigned_path = str(large.resolve())
        reference_path = str(reference.resolve())
        self.assertTrue(scope["implementation_mode"])
        self.assertEqual(scope["implementation_tool_limits"], {"Read": 10, "Grep": 3})
        self.assertEqual(scope["range_restricted"], [reference_path])
        self.assertIsNone(tool_scope_guard.authorize(
            "Read", {"file_path": assigned_path, "offset": 1, "limit": 80}, scope))
        self.assertIsNotNone(tool_scope_guard.authorize(
            "Read", {"file_path": reference_path, "offset": 1, "limit": 80}, scope))
        allowed = autonomous.implementation_reference_ranges(self.workspace, step, context)
        self.assertEqual(allowed, {"src/reference.js": [{"offset": 40, "limit": 30}]})

    def test_implementation_pretool_hook_hard_caps_reads_and_does_not_charge_denials(self):
        source = self.workspace / "src" / "implement.js"
        source.write_text("line\\n" * 450, encoding="utf-8")
        counter_dir = self.runs_root / "implementation-calls"
        counter_dir.mkdir()
        scope_path = self.runs_root / "implementation.scope.json"
        scope_path.write_text(json.dumps({
            "root": str(self.workspace), "read_allowed": ["src/implement.js"],
            "write_allowed": ["src/implement.js"], "implementation_mode": True,
            "implementation_call_counter_dir": str(counter_dir),
            "implementation_tool_limits": {"Read": 2, "Grep": 1},
            "implementation_denied_attempt_limit": 3, "range_restricted": [],
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")), str(scope_path)]

        def invoke(tool, tool_input):
            return subprocess.run(hook, input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                                  text=True, capture_output=True, check=True)

        denied_unauthorized = invoke("Read", {"file_path": str(self.workspace / "src" / "other.js"),
                                                "offset": 1, "limit": 80})
        allowed_one = invoke("Read", {"file_path": str(source.resolve()), "offset": 1, "limit": 80})
        allowed_two = invoke("Read", {"file_path": str(source.resolve()), "offset": 81, "limit": 80})
        over_quota = invoke("Read", {"file_path": str(source.resolve()), "offset": 161, "limit": 80})
        self.assertIn("exact assigned", denied_unauthorized.stdout)
        self.assertEqual(allowed_one.stdout, "")
        self.assertEqual(allowed_two.stdout, "")
        self.assertIn("edit the assigned files now", over_quota.stdout)
        self.assertEqual(len(list(counter_dir.glob("read-*.ticket"))), 2)
        self.assertEqual(len(list(counter_dir.glob("total-*.ticket"))), 2)
        self.assertEqual(len(list(counter_dir.glob("denied-*.ticket"))), 2)

    def test_implementation_pretool_hook_requires_early_edit_before_more_reads(self):
        source = self.workspace / "src" / "implement.js"
        source.write_text("module.exports = 1;\\n", encoding="utf-8")
        counter_dir = self.runs_root / "implementation-early-edit"
        counter_dir.mkdir()
        scope_path = self.runs_root / "implementation-early-edit.scope.json"
        scope_path.write_text(json.dumps({
            "root": str(self.workspace), "read_allowed": ["src/implement.js"],
            "write_allowed": ["src/implement.js"], "implementation_mode": True,
            "implementation_call_counter_dir": str(counter_dir),
            "implementation_tool_limits": {"Read": 5, "Grep": 2},
            "implementation_pre_edit_read_limit": 2,
            "implementation_denied_attempt_limit": 3, "range_restricted": [],
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")), str(scope_path)]

        def invoke(tool, tool_input):
            return subprocess.run(hook, input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                                  text=True, capture_output=True, check=True)

        read = {"file_path": str(source.resolve())}
        self.assertEqual(invoke("Read", read).stdout, "")
        self.assertEqual(invoke("Read", read).stdout, "")
        blocked = invoke("Read", read)
        self.assertIn("2 Reads before the first edit", blocked.stdout)
        self.assertEqual(invoke("Edit", {"file_path": str(source.resolve())}).stdout, "")
        self.assertEqual(invoke("Read", read).stdout, "")
        self.assertTrue((counter_dir / "first-edit.ticket").exists())

    def test_inspection_handoff_rejects_read_outside_caller_declared_range(self):
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 800, encoding="utf-8")
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": "src/large.js", "symbols": ["route"],
                             "ranges": [{"offset": 401, "limit": 20, "purpose": "route"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/large.js", "offset": 401, "limit": 20}]}
        with self.assertRaisesRegex(ValueError, "exceeds caller-declared source ranges"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/large.js"], audit,
                required_source_paths=["src/large.js"],
                caller_source_ranges=[{"path": "src/large.js", "offset": 251, "limit": 125}])

    def test_inspection_handoff_clamps_range_past_end_of_file(self):
        small = self.workspace / "src" / "small.js"
        small.write_text("line\n" * 12, encoding="utf-8")
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": "src/small.js", "symbols": ["route"],
                             "ranges": [{"offset": 10, "limit": 5, "purpose": "route"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/small.js", "offset": 10, "limit": 5}]}
        validated = autonomous.validate_inspection_handoff(
            json.dumps(report), self.workspace, ["src/small.js"], audit)
        self.assertEqual(validated["files"][0]["ranges"][0]["limit"], 3)

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
        self.assertTrue(scope["result_cap_enabled"])
        self.assertEqual(scope["result_char_limit"], 3500)
        self.assertEqual(autonomous.SOURCE_READ_RESULT_CHAR_LIMIT, 4000)
        self.assertEqual(autonomous.SOURCE_SCOUT_TIMEOUT, 420)
        self.assertEqual(settings["hooks"]["PostToolUse"][0]["matcher"], "Read|Grep")
        self.assertEqual(settings["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"], 30)
        (self.workspace / "src" / "other.py").write_text("secret\n", encoding="utf-8")
        hook_command = [sys.executable, str(Path(autonomous.__file__).with_name("tool_scope_guard.py")),
                        str(scope_path)]
        denied = subprocess.run(hook_command, input=json.dumps({
            "tool_name": "Read", "tool_input": {"file_path": str(self.workspace / "src/other.py")},
        }), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_result_guard_caps_string_and_preserves_nested_native_shape(self):
        marker_id = "smoke-42"
        output, audit = tool_result_guard.cap_output("x" * 12000, marker_id=marker_id)
        self.assertIsInstance(output, str)
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=False, separators=(",", ":"))), 4000)
        self.assertIn(marker_id, output)
        self.assertTrue(audit["truncated"])

        native = {"content": [{"type": "text", "text": ("服務\r\n" * 2000)}],
                  "metadata": {"source": "Read", "line": 1}}
        capped, nested_audit = tool_result_guard.cap_output(native, marker_id=marker_id)
        self.assertIsInstance(capped, dict)
        self.assertIsInstance(capped["content"], list)
        self.assertEqual(capped["content"][0]["type"], "text")
        self.assertEqual(capped["metadata"], native["metadata"])
        self.assertLessEqual(len(json.dumps(capped, ensure_ascii=False, separators=(",", ":"))), 4000)
        self.assertIn(marker_id, capped["content"][0]["text"])
        self.assertTrue(nested_audit["truncated"])

        multi = {"content": [{"type": "text", "text": "A" * 8000},
                              {"type": "text", "text": "tail"}],
                 "filePath": "large.txt", "numLines": 2}
        multi_capped, _ = tool_result_guard.cap_output(multi, marker_id=marker_id)
        self.assertEqual([item["type"] for item in multi_capped["content"]], ["text", "text"])
        self.assertEqual(multi_capped["filePath"], "large.txt")
        self.assertEqual(multi_capped["numLines"], 2)
        self.assertIn(marker_id, multi_capped["content"][0]["text"])
        self.assertEqual(multi_capped["content"][1]["text"], "")
        self.assertLessEqual(len(json.dumps(multi_capped, ensure_ascii=False,
                                           separators=(",", ":"))), 4000)

    def test_result_guard_leaves_small_output_unchanged_and_hook_audits_without_content(self):
        audit_path = self.runs_root / "result-audit.jsonl"
        scope_path = self.runs_root / "result-scope.json"
        scope_path.write_text(json.dumps({
            "result_cap_enabled": True, "result_char_limit": 4000,
            "result_marker_id": "run-99", "result_audit_path": str(audit_path),
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(tool_result_guard.__file__)), str(scope_path)]
        small = {"content": [{"type": "text", "text": "short"}]}
        result = subprocess.run(hook, input=json.dumps({
            "tool_name": "Read", "tool_use_id": "read-1", "tool_response": small,
        }), text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, "")
        receipt = next((audit_path.with_name(audit_path.name + ".receipts")).glob("*.json"))
        record = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertFalse(record["changed"])
        self.assertEqual(record["tool_use_id"], "read-1")
        self.assertNotIn("short", json.dumps(record))

    def test_result_guard_hook_rewrites_large_grep_output_with_same_root_shape(self):
        audit_path = self.runs_root / "large-grep-audit.jsonl"
        scope_path = self.runs_root / "large-grep-scope.json"
        scope_path.write_text(json.dumps({
            "result_cap_enabled": True, "result_char_limit": 4000,
            "result_marker_id": "run-grep-4", "result_audit_path": str(audit_path),
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(tool_result_guard.__file__)), str(scope_path)]
        original = {"content": [{"type": "text", "text": "match " + "x" * 180} for _ in range(100)],
                    "filePath": "large.txt", "numLines": 100}
        result = subprocess.run(hook, input=json.dumps({
            "tool_name": "Grep", "tool_use_id": "grep-1", "tool_response": original,
        }), text=True, capture_output=True, check=True)
        replacement = json.loads(result.stdout)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(replacement, dict)
        self.assertEqual(replacement["filePath"], "large.txt")
        self.assertEqual(replacement["numLines"], 100)
        self.assertTrue(all(item["type"] == "text" for item in replacement["content"]))
        self.assertLessEqual(len(json.dumps(replacement, ensure_ascii=False, separators=(",", ":"))), 4000)
        receipt = next((audit_path.with_name(audit_path.name + ".receipts")).glob("*.json"))
        record = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertTrue(record["truncated"])
        self.assertEqual(record["tool_use_id"], "grep-1")
        self.assertIn(f"[QWEN_RESULT_TRUNCATED tool=Grep id={record['marker_id']};",
                      json.dumps(replacement))

    def test_parallel_result_hooks_persist_every_unique_receipt(self):
        audit_path = self.runs_root / "parallel-result-audit.jsonl"
        scope_path = self.runs_root / "parallel-result-scope.json"
        scope_path.write_text(json.dumps({
            "result_cap_enabled": True, "result_char_limit": 4000,
            "result_audit_path": str(audit_path),
        }), encoding="utf-8")
        hook = [sys.executable, str(Path(tool_result_guard.__file__)), str(scope_path)]
        def invoke(index):
            return subprocess.run(hook, input=json.dumps({
                "tool_name": "Read", "tool_use_id": f"parallel-{index}",
                "tool_response": {"content": [{"type": "text", "text": f"result-{index}"}]},
            }), text=True, capture_output=True, check=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(invoke, range(24)))
        self.assertTrue(all(result.stdout == "" for result in results))
        receipts = list((audit_path.with_name(audit_path.name + ".receipts")).glob("*.json"))
        records = [json.loads(receipt.read_text(encoding="utf-8")) for receipt in receipts]
        self.assertEqual(len(records), 24)
        self.assertEqual({record["tool_use_id"] for record in records},
                         {f"parallel-{index}" for index in range(24)})

    def test_result_cap_audit_requires_receipts_and_checks_cli_delivered_text(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "read-1", "name": "Read", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "read-1", "is_error": False,
                 "content": "bounded"}]}},
        ]
        audit_path = self.runs_root / "missing-cap-receipt.jsonl"
        audit = autonomous.audit_tool_result_caps(events, audit_path, 4000)
        self.assertFalse(audit["within_budget"])
        self.assertEqual(audit["violations"][0]["reason"], "tool_result_cap_receipt_missing")

    def test_result_cap_audit_rejects_truncation_without_marker_in_transcript(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "read-2", "name": "Read", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "read-2", "is_error": False,
                 "content": "text was truncated but no sentinel"}]}},
        ]
        audit_path = self.runs_root / "bad-marker-receipt.jsonl"
        audit_path.write_text(json.dumps({"tool_use_id": "read-2", "changed": True,
                                          "tool": "Read",
                                          "truncated": True, "marker_id": "marker-x",
                                          "delivered_chars": 40}) + "\n", encoding="utf-8")
        audit = autonomous.audit_tool_result_caps(events, audit_path, 4000)
        self.assertFalse(audit["within_budget"])
        self.assertEqual(audit["violations"][0]["reason"],
                         "truncation_marker_missing_from_delivered_result")

    def test_inspection_context_uses_runner_verified_tool_marker_mapping(self):
        report = {"summary": "Found source", "implementation_plan": "Continue",
                  "files": [], "risks": []}
        read_audit = {"result_cap_audit": {"results": [
            {"tool": "Read", "receipt": {"marker_id": "read-id", "original_chars": 12000,
                                               "delivered_chars": 4000, "truncated": True}},
            {"tool": "Grep", "receipt": {"marker_id": "grep-id", "original_chars": 18000,
                                               "delivered_chars": 3990, "truncated": True}},
            {"tool_use_id": "small-read", "tool": "Read", "path": "src/app.py",
             "offset": 1, "limit": 20,
             "receipt": {"marker_id": "phantom-id", "original_chars": 400,
                         "delivered_chars": 400, "truncated": False}},
        ]}}
        enriched = autonomous.attach_verified_result_caps(report, read_audit)
        self.assertEqual(enriched["runner_verified_result_caps"], [
            {"tool_use_id": None, "tool": "Read", "path": None, "offset": None, "limit": None,
             "pattern": None, "head_limit": None, "marker_id": "read-id", "original_chars": 12000,
             "delivered_chars": 4000, "truncated": True},
            {"tool_use_id": None, "tool": "Grep", "path": None, "offset": None, "limit": None,
             "pattern": None, "head_limit": None, "marker_id": "grep-id", "original_chars": 18000,
             "delivered_chars": 3990, "truncated": True},
            {"tool_use_id": "small-read", "tool": "Read", "path": "src/app.py", "offset": 1,
             "limit": 20, "pattern": None, "head_limit": None, "marker_id": None,
             "original_chars": 400, "delivered_chars": 400, "truncated": False},
        ])
        step = {"id": "step-test", "title": "Implement with verified source map", "task": "Implement",
                "files": ["src/app.py"], "reference_paths": [], "source_ranges": [], "covers": [], "checks": []}
        prompt = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [], inspection_context=enriched
        ))[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("tool-to-marker mapping comes from the CLI transcript and hook audit", prompt)
        self.assertIn('"marker_id": "read-id"', prompt)
        self.assertIn('"marker_id": "grep-id"', prompt)

    def test_result_cap_mapping_preserves_source_path_range_and_query(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "r1", "name": "Read",
                 "input": {"file_path": str(self.workspace / "src" / "app.py"), "offset": 10, "limit": 30}},
                {"type": "tool_use", "id": "g1", "name": "Grep",
                 "input": {"path": str(self.workspace / "src"), "pattern": "TODO", "head_limit": 8}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1", "is_error": False,
                 "content": "[QWEN_RESULT_TRUNCATED tool=Read id=marker-r; original_chars=9000; limit=4000. retry]"},
                {"type": "tool_result", "tool_use_id": "g1", "is_error": False,
                 "content": "[QWEN_RESULT_TRUNCATED tool=Grep id=marker-g; original_chars=8000; limit=4000. retry]"},
            ]}},
        ]
        audit_path = self.runs_root / "source-map.jsonl"
        audit_path.write_text("".join(json.dumps(record) + "\n" for record in [
            {"tool_use_id": "r1", "tool": "Read", "changed": True, "truncated": True,
             "marker_id": "marker-r", "original_chars": 9000, "delivered_chars": 100},
            {"tool_use_id": "g1", "tool": "Grep", "changed": True, "truncated": True,
             "marker_id": "marker-g", "original_chars": 8000, "delivered_chars": 100},
        ]), encoding="utf-8")
        audit = autonomous.audit_tool_result_caps(events, audit_path, 4000, self.workspace)
        self.assertTrue(audit["within_budget"], audit["violations"])
        by_tool = {item["tool"]: item for item in audit["results"]}
        self.assertEqual((by_tool["Read"]["path"], by_tool["Read"]["offset"],
                          by_tool["Read"]["limit"]), ("src/app.py", 10, 30))
        self.assertEqual((by_tool["Grep"]["path"], by_tool["Grep"]["pattern"],
                          by_tool["Grep"]["head_limit"]), ("src", "TODO", 8))

    def test_result_cap_audit_rejects_duplicate_ids_receipts_and_tool_mismatch(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "same", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "same", "name": "Grep", "input": {}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "same", "is_error": False, "content": "ok"},
                {"type": "tool_result", "tool_use_id": "same", "is_error": False, "content": "ok"},
            ]}},
        ]
        audit_path = self.runs_root / "duplicate-receipts.jsonl"
        audit_path.write_text("".join(json.dumps(record) + "\n" for record in [
            {"tool_use_id": "same", "tool": "Read", "changed": False, "delivered_chars": 4},
            {"tool_use_id": "same", "tool": "Grep", "changed": False, "delivered_chars": 4},
            {"tool_use_id": "orphan", "tool": "Read", "changed": False, "delivered_chars": 4},
        ]), encoding="utf-8")
        audit = autonomous.audit_tool_result_caps(events, audit_path, 4000, self.workspace)
        reasons = {item["reason"] for item in audit["violations"]}
        self.assertIn("tool_result_tool_use_id_duplicate", reasons)
        self.assertIn("tool_result_success_duplicate", reasons)
        self.assertIn("tool_result_cap_receipt_duplicate", reasons)
        self.assertIn("tool_result_cap_tool_mismatch", reasons)
        self.assertIn("tool_result_cap_receipt_unmatched", reasons)

    def test_result_cap_audit_rejects_reused_marker_ids(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "r2", "name": "Read", "input": {}},
        ]}}, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": False,
             "content": f"[QWEN_RESULT_TRUNCATED tool=Read id=dup; original_chars=9000; limit=4000. retry]"}
            for tool_id in ("r1", "r2")
        ]}}]
        audit_path = self.runs_root / "duplicate-markers.jsonl"
        audit_path.write_text("".join(json.dumps({"tool_use_id": tool_id, "tool": "Read",
            "changed": True, "truncated": True, "marker_id": "dup", "delivered_chars": 100}) + "\n"
            for tool_id in ("r1", "r2")), encoding="utf-8")
        audit = autonomous.audit_tool_result_caps(events, audit_path, 4000, self.workspace)
        self.assertIn("tool_result_cap_marker_id_duplicate",
                      {item["reason"] for item in audit["violations"]})

    def test_result_cap_audit_checks_error_results_too(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "err-read", "name": "Read", "input": {
                "file_path": "src/app.py", "offset": 1, "limit": 200}},
        ]}}, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "err-read", "is_error": True,
             "content": "x" * 4100},
        ]}}]
        audit_path = self.runs_root / "error-result-cap.jsonl"
        audit_path.write_text(json.dumps({"tool_use_id": "err-read", "tool": "Read",
            "cap_failed": True, "changed": False, "delivered_chars": 4100}) + "\n", encoding="utf-8")
        audit = autonomous.audit_tool_result_caps(events, audit_path, 4000, self.workspace)
        self.assertEqual(audit["error_results"], 1)
        self.assertFalse(audit["within_budget"])
        self.assertIn("tool_result_cap_failed", {item["reason"] for item in audit["violations"]})

    def test_result_cap_audit_exempts_pretool_scope_guard_denial(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "denied-read", "name": "Read", "input": {
                "file_path": "src/app.py", "offset": 1, "limit": 200}},
        ]}}, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "denied-read", "is_error": True,
             "content": "QWEN_SCOPE_GUARD_DENY: exact assigned files only"},
        ]}}]
        audit = autonomous.audit_tool_result_caps(
            events, self.runs_root / "scope-deny-no-receipt.jsonl", 4000, self.workspace)
        self.assertTrue(audit["within_budget"], audit["violations"])
        self.assertEqual(audit["scope_guard_denials"], 1)
        self.assertEqual(audit["tool_results"], 0)

        for content in (
            "Error echoes QWEN_SCOPE_GUARD_DENY: from unrelated output",
            "QWEN_SCOPE_GUARD_DENY: " + "x" * autonomous.MAX_SCOPE_GUARD_DENIAL_CHARS,
        ):
            with self.subTest(content_length=len(content)):
                events[1]["message"]["content"][0]["content"] = content
                rejected = autonomous.audit_tool_result_caps(
                    events, self.runs_root / "scope-deny-untrusted.jsonl", 4000, self.workspace)
                self.assertFalse(rejected["within_budget"])
                self.assertIn("tool_result_cap_receipt_missing",
                              {item["reason"] for item in rejected["violations"]})

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
        self.assertIn("10 Read calls", initial)
        self.assertIn("3 combined Grep/Glob calls", initial)
        self.assertIn("13 combined inspection calls", initial)
        self.assertIn("Begin editing after at most four targeted Read calls", initial)

        continuation = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [],
            repair_feedback="Remaining step inspection budget: 5 Reads, 4 searches, 7 combined calls.",
            read_only_paths=["test/test_app.py"],
        ))[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("Do not reread them automatically", continuation)
        self.assertIn("Remaining step inspection budget: 5 Reads", continuation)
        self.assertIn(str((self.workspace / "test/test_app.py").resolve()), continuation)
        self.assertNotIn("Read these caller-supplied acceptance and supporting reference files before editing", continuation)

    def test_source_scout_coordinator_request_is_read_only_and_phase_specific(self):
        step = {"id": "step-scout", "title": "Find API pattern", "task": "Add an endpoint",
                "files": ["src/app.py"], "reference_paths": ["test/test_app.py"],
                "checks": [], "covers": []}
        prompt = autonomous.coordinator_prompt(
            step, self.workspace, "MARKER", False, phase="scout")
        self.assertIn("foreground read-only source inspection", prompt)
        self.assertIn("Do not ask it to implement the task", prompt)
        self.assertIn("Add an endpoint", prompt)
        self.assertNotIn("foreground complete task", prompt)
        child_prompt = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", [], phase="scout"))[
                autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("Return exactly one JSON object followed by the completion marker", child_prompt)
        self.assertIn("append the exact marker MARKER on its own final line", child_prompt)
        self.assertIn("This marker completes the inspection handoff; it does not claim that implementation is complete.",
                      child_prompt)
        self.assertIn('"files": [{"path": workspace-relative POSIX path', child_prompt)

        implementation_prompt = json.loads(autonomous.step_agent_spec(
            step, "Request", [], self.workspace, "MARKER", []))[
                autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn("End with MARKER only after the implementation step is complete.", implementation_prompt)

    def test_implementation_coordinator_must_forward_verified_source_map_to_worker(self):
        step = {"id": "step-1", "title": "Implement route", "task": "Add endpoint",
                "files": ["src/server.js"], "reference_paths": ["test/route.test.js"],
                "checks": ["route works"], "covers": []}
        context = {"summary": "Route is in the service handler.", "files": [
            {"path": "src/server.js", "ranges": [{"offset": 30, "limit": 20,
                                                     "purpose": "service routes"}]}]}
        prompt = autonomous.coordinator_prompt(
            step, self.workspace, "MARKER", False, phase="implementation",
            inspection_context=context)
        self.assertIn("complete verified source map below in its Agent prompt", prompt)
        self.assertIn("per-child hard ceiling of 10 Read calls and 3 Grep calls", prompt)
        self.assertIn(json.dumps(context, ensure_ascii=False), prompt)
        self.assertIn("copy verbatim into the Agent prompt", prompt)

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
                             "ranges": [{"offset": 1, "limit": 1, "purpose": "route handler"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/app.py", "offset": 1, "limit": 1}]}
        accepted = autonomous.validate_inspection_handoff(
            json.dumps(report), self.workspace, ["src/app.py"], audit)
        self.assertEqual(accepted["files"][0]["path"], "src/app.py")
        self.assertEqual(autonomous.validate_inspection_handoff(
            json.dumps(report) + "\nQWEN_AUTONOMOUS_COMPLETE_abc123", self.workspace,
            ["src/app.py"], audit)["summary"], "Found route")
        wrapped_text = autonomous.tool_result_text({"content": [{"type": "text", "text":
            json.dumps(report) + "\nQWEN_AUTONOMOUS_COMPLETE_abc123agentId: worker <usage>tokens</usage>"}]})
        self.assertEqual(autonomous.validate_inspection_handoff(
            wrapped_text, self.workspace, ["src/app.py"], audit)["summary"], "Found route")
        cli_wrapped = ("```json\n" + json.dumps(report) + "\nQWEN_AUTONOMOUS_COMPLETE_abc123\n```")
        cli_wrapped += "\nagentId: adbb73f41a2279bdd (use SendMessage with to: 'worker')\n"
        cli_wrapped += "<usage>subagent_tokens: 20\ntool_uses: 1\nduration_ms: 10</usage>"
        self.assertEqual(autonomous.validate_inspection_handoff(
            cli_wrapped, self.workspace, ["src/app.py"], audit)["summary"], "Found route")
        expanded = {**report, "summary": "s" * 1_674, "implementation_plan": "p" * 2_000,
                    "risks": ["Some relevant risk " * 20],
                    "files": [{**report["files"][0], "ranges": [{**report["files"][0]["ranges"][0],
                                                                       "purpose": "Relevant source details " * 12}]}]}
        expanded_result = autonomous.validate_inspection_handoff(
            json.dumps(expanded) + "\nQWEN_AUTONOMOUS_COMPLETE_abc123agentId: test <usage>tokens</usage>",
            self.workspace, ["src/app.py"], audit)
        self.assertEqual(len(expanded_result["summary"]), 1_674)
        self.assertLessEqual(len(expanded_result["files"][0]["ranges"][0]["purpose"]), 120)
        self.assertLessEqual(len(expanded_result["risks"][0]), 200)
        report["files"][0]["ranges"][0]["limit"] = 201
        with self.assertRaisesRegex(ValueError, "ranged evidence"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/app.py"], audit)
        report["files"][0]["ranges"][0]["limit"] = 1
        with self.assertRaisesRegex(ValueError, "not read"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/app.py"], {"reads": []})
        with self.assertRaisesRegex(ValueError, "range was not present"):
            autonomous.validate_inspection_handoff(
                json.dumps(report), self.workspace, ["src/app.py"],
                {"reads": [{"tool": "Read", "path": "src/app.py"}]})

    def test_inspection_handoff_rejects_oversized_and_nonrelative_reports_clearly(self):
        self.assertEqual(autonomous.SOURCE_SCOUT_HANDOFF_CHAR_LIMIT, 12000)
        with self.assertRaisesRegex(ValueError, "maximum is 12000"):
            autonomous.validate_inspection_handoff("x" * 12001, self.workspace, [], {})
        with self.assertRaisesRegex(ValueError, "concise summary"):
            autonomous.validate_inspection_handoff("Here is the report:\n{}", self.workspace, [], {})
        report = {"summary": "Found route", "implementation_plan": "Update handler",
                  "files": [{"path": str(self.workspace / "src" / "app.py"), "symbols": [],
                             "ranges": [{"offset": 1, "limit": 1, "purpose": "handler"}]}]}
        with self.assertRaisesRegex(ValueError, "invalid relative path|clean workspace-relative path"):
            autonomous.validate_inspection_handoff(json.dumps(report), self.workspace,
                                                    ["src/app.py"], {"reads": []})

    def test_inspection_handoff_trims_overlong_summary_and_plan_after_audit(self):
        source = self.workspace / "src" / "app.js"
        source.write_text("module.exports = {};\n", encoding="utf-8")
        report = {"summary": "summary " * 600, "implementation_plan": "plan " * 600,
                  "files": [{"path": "src/app.js", "symbols": ["module.exports"],
                             "ranges": [{"offset": 1, "limit": 1, "purpose": "module entry"}]}]}
        audit = {"reads": [{"tool": "Read", "path": "src/app.js", "offset": 1, "limit": 1}]}
        normalized = autonomous.validate_inspection_handoff(
            json.dumps(report), self.workspace, ["src/app.js"], audit)
        self.assertLessEqual(len(normalized["summary"]), autonomous.SOURCE_SCOUT_SUMMARY_CHAR_LIMIT)
        self.assertLessEqual(len(normalized["implementation_plan"]), 2_200)

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
        (self.workspace / "src" / "app.py").write_text("value = 1\nother = 2\n", encoding="utf-8")
        report["files"][0]["ranges"][0]["offset"] = 2
        with self.assertRaisesRegex(ValueError, "caller-declared|range was not present"):
            autonomous.validate_preverified_inspection(report, step, self.workspace, [])

    def test_extracts_only_json_and_rejects_unstructured_output(self):
        value = autonomous.extract_json('Here is the plan:\n```json\n{"steps": []}\n```')
        self.assertEqual(value, {"steps": []})
        with self.assertRaises(ValueError):
            autonomous.extract_json("I will do that now.")

    def test_tool_result_text_extracts_text_blocks_without_python_list_repr(self):
        result = {"content": [{"type": "text", "text": '{"summary":"ok"}'},
                             {"type": "image", "data": "ignored"}]}
        self.assertEqual(autonomous.tool_result_text(result), '{"summary":"ok"}')

    def test_qwen_route_verification_requires_qwen_sift_and_available_telemetry(self):
        self.assertTrue(autonomous.qwen_route_verification("available", 2, 2))
        self.assertFalse(autonomous.qwen_route_verification("available", 0, 2))
        self.assertFalse(autonomous.qwen_route_verification("available", 2, 0))
        self.assertFalse(autonomous.qwen_route_verification("available", None, 2))
        self.assertFalse(autonomous.qwen_route_verification("unavailable", None, None))
        self.assertTrue(autonomous.qwen_route_verification(
            "unavailable", None, None, transcript_qwen=True,
            gateway_base_url=autonomous.dispatch.BASE_URL, sift_hook_configured=True))
        self.assertFalse(autonomous.qwen_route_verification(
            "unavailable", None, None, transcript_qwen=False,
            gateway_base_url=autonomous.dispatch.BASE_URL, sift_hook_configured=True))
        self.assertFalse(autonomous.qwen_route_verification(
            "unavailable", None, None, transcript_qwen=True,
            gateway_base_url="http://127.0.0.1:9999", sift_hook_configured=True))
        self.assertFalse(autonomous.qwen_route_verification(
            "unavailable", None, None, transcript_qwen=True,
            gateway_base_url=autonomous.dispatch.BASE_URL, sift_hook_configured=False))

    def test_repair_source_context_keeps_only_successful_allowed_mapped_reads(self):
        source = self.workspace / "src" / "app.py"
        assistant = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "read-good", "name": "Read",
             "input": {"file_path": str(source), "offset": 2, "limit": 2}},
            {"type": "tool_use", "id": "read-denied", "name": "Read",
             "input": {"file_path": str(source), "offset": 8, "limit": 2}},
            {"type": "tool_use", "id": "read-other", "name": "Read",
             "input": {"file_path": str(self.workspace / "test/test_app.py"), "offset": 1, "limit": 2}},
        ]}}
        results = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-good", "content": "2\tdef render():\n3\t    return 'home'"},
            {"type": "tool_result", "tool_use_id": "read-denied", "is_error": True,
             "content": "QWEN_SCOPE_GUARD_DENY: out of verified range"},
            {"type": "tool_result", "tool_use_id": "read-other", "content": "unassigned test source"},
        ]}}
        context = autonomous.read_context_from_events(
            [assistant, results], self.workspace,
            allowed_paths=["src/app.py"],
            allowed_ranges={"src/app.py": [{"offset": 1, "limit": 4}]},
        )
        self.assertIn("src/app.py lines 2-3", context)
        self.assertIn("return 'home'", context)
        self.assertNotIn("out of verified range", context)
        self.assertNotIn("unassigned test source", context)

    def test_repair_source_context_prioritizes_step_files_over_earlier_reference_reads(self):
        source = self.workspace / "src" / "app.py"
        reference = self.workspace / "test" / "test_app.py"
        assistant = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "read-reference", "name": "Read",
             "input": {"file_path": str(reference), "offset": 1, "limit": 2}},
            {"type": "tool_use", "id": "read-source", "name": "Read",
             "input": {"file_path": str(source), "offset": 2, "limit": 2}},
        ]}}
        results = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-reference", "content": "reference assertion"},
            {"type": "tool_result", "tool_use_id": "read-source", "content": "target implementation"},
        ]}}
        context = autonomous.read_context_from_events(
            [assistant, results], self.workspace,
            allowed_paths=["src/app.py", "test/test_app.py"], max_chars=300)
        self.assertLess(context.index("src/app.py"), context.index("test/test_app.py"))
        self.assertIn("target implementation", context)

    def test_scoped_workspace_diff_is_limited_and_matches_current_target(self):
        source = self.workspace / "src" / "app.py"
        before = {"src/app.py": source.read_text(encoding="utf-8")}
        source.write_text(before["src/app.py"] + "# targeted change\n", encoding="utf-8")
        diff = autonomous.scoped_workspace_diff(before, self.workspace, ["src/app.py"])
        self.assertIn("src/app.py", diff)
        self.assertIn("+# targeted change", diff)
        self.assertNotIn("test/test_app.py", diff)
        self.assertLessEqual(len(autonomous.scoped_workspace_diff(
            {"src/app.py": "old\n"}, self.workspace, ["src/app.py"], max_chars=100)), 100)

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
        self.assertEqual(audit["read_count"], 0)
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
        self.assertEqual(autonomous.CHILD_STEP_TIMEOUT, 600)
        self.assertEqual(autonomous.CHILD_IDLE_TIMEOUT, 300)
        self.assertEqual(autonomous.INSPECTION_PHASE_LIMITS,
                         {"reads": 18, "searches": 18, "inspections": 36})
        self.assertEqual(autonomous.IMPLEMENTATION_INITIAL_LIMITS,
                         {"reads": 10, "searches": 3, "inspections": 13})
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
        self.assertEqual(audit["read_count"], 0)
        self.assertEqual(audit["search_count"], 2)
        self.assertEqual(audit["counts_by_tool"], {"Read": 0, "Grep": 1, "Glob": 1})
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
            "source_ranges": [{"path": "src/app.py", "offset": 1, "limit": 1,
                               "purpose": "Existing implementation entry point"}],
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

    def test_run_request_uses_audited_source_scout_for_large_unhinted_file(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        large = self.workspace / "src" / "large.js"
        large.write_text("line\n" * 450, encoding="utf-8")
        requirements = [{"id": "req-feature", "acceptance": "The large-source feature is implemented",
                         "validator_indices": [0], "step_validator_indices": [0]}]
        plan = {"summary": "One bounded source step", "steps": [{
            "id": "step-feature", "title": "Implement large-source feature", "task": "Implement it",
            "files": ["src/large.js"], "depends_on": [], "checks": ["Feature is present"],
            "covers": ["req-feature"],
        }]}

        class Response:
            def __init__(self, value=None): self.value = value
            def raise_for_status(self): return None
            def json(self): return self.value

        active = {"active_model": "qwen", "mode": "alias", "reasoning_effort": "high",
                  "advisor_model": None, "advisor_reasoning_effort": None,
                  "advisor_sensitivity": "medium", "advisor_trigger": None,
                  "advisor_classifier": None, "fast_mode": False}

        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def get(self, path): return Response(active if path == "/api/active-model" else {})
            def post(self, *args, **kwargs): return Response({})

        scout_report = {"summary": "Found the module entry point.",
                        "implementation_plan": "Edit the assigned module only.", "risks": [],
                        "files": [{"path": "src/large.js", "symbols": ["entry"],
                                   "ranges": [{"offset": 220, "limit": 25,
                                               "purpose": "Implementation entry point"}]}]}
        audit = {"read_count": 1, "search_count": 1, "inspection_count": 2,
                 "counts_by_tool": {"Read": 1, "Grep": 1, "Glob": 0},
                 "reads": [{"tool": "Read", "path": "src/large.js", "offset": 220,
                            "limit": 25, "line_count_capped": 401}],
                 "violations": [], "within_budget": True}
        phases = []
        child_timeouts = []

        def child(workspace, step, request, requirements, key, previous, **kwargs):
            phase = kwargs.get("phase")
            phases.append(phase)
            child_timeouts.append(kwargs["timeout"])
            if phase == "scout":
                return {"phase": "scout", "success": True, "exit": 0, "stop_reason": None,
                        "seconds": 1, "child_tool_calls": ["Grep", "Read"], "qwen_route_calls": 1,
                        "child_handoff": True, "child_result": "source map", "final": "scouted",
                        "inspection_report": scout_report, "read_audit": audit}
            self.assertEqual(phase, "implementation")
            self.assertEqual(kwargs["inspection_context"]["files"][0]["ranges"][0]["offset"], 220)
            with large.open("a", encoding="utf-8") as target: target.write("feature = true\n")
            return {"phase": phase, "success": True, "exit": 0, "stop_reason": None,
                    "seconds": 1, "child_tool_calls": ["Edit"], "qwen_route_calls": 1,
                    "child_handoff": True, "child_result": "implemented", "final": "complete",
                    "read_audit": {"read_count": 0, "search_count": 0, "inspection_count": 0,
                                   "counts_by_tool": {"Read": 0, "Grep": 0, "Glob": 0},
                                   "reads": [], "violations": [], "within_budget": True}}

        passed = [{"argv": ["acceptance"], "exit": 0, "stdout": "passed", "stderr": ""}]
        with mock.patch.object(autonomous, "run_validators", return_value=passed), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous, "run_child", side_effect=child):
            result = autonomous.run_request(
                self.workspace, "Implement a large-source feature", [["acceptance"]],
                runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                validator_timeout=5, baseline_validators=[["baseline"]],
                requirements=requirements, plan_override=plan,
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(phases, ["scout", "implementation"])
        self.assertEqual(child_timeouts, [5, 5])
        self.assertEqual(result["steps"][0]["inspection"]["source"], "qwen_bounded_source_scout")
        self.assertTrue(result["steps"][0]["inspection"]["status"] == "passed")

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
                self.assertNotIn("Checkpoint:", kwargs["repair_feedback"])
                self.assertIn("Resume instructions from the saved checkpoint: Continue in the same persistent workspace.",
                              kwargs["repair_feedback"])
                self.assertIn("Remaining step inspection budget: 24 Reads, 17 searches, 41 combined calls.",
                              kwargs["repair_feedback"])
                self.assertIn("Remaining implementation-phase inspection budget: 19 Reads, 14 searches, 33 combined calls.",
                              kwargs["repair_feedback"])
                self.assertIn("test/test_app.py", kwargs["repair_feedback"])
                self.assertNotIn("Read the listed acceptance paths first", kwargs["repair_feedback"])
                self.assertNotIn("Read the acceptance files first", kwargs["repair_feedback"])
                self.assertIn("24 Reads, 17 searches, 41 combined calls", kwargs["repair_feedback"])
                self.assertIn("Successful source excerpts captured from the first child", kwargs["repair_feedback"])
                self.assertIn("def render(): return 'home'", kwargs["repair_feedback"])
                self.assertIn("Current step-owned diff relative to the pre-worker files", kwargs["repair_feedback"])
                self.assertIn("+# attempt 1", kwargs["repair_feedback"])
                self.assertIn("Preserve the existing scoped diff", kwargs["repair_feedback"])
                self.assertIn("Do not read runner checkpoints", kwargs["repair_feedback"])
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
                    "read_context": "src/app.py lines 1-2\ndef render(): return 'home'",
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
        self.assertEqual(first_kwargs["inspection_limits"], {"reads": 10, "searches": 3, "inspections": 13})
        self.assertEqual(continuation_kwargs["inspection_limits"], {"reads": 19, "searches": 14, "inspections": 33})
        self.assertEqual(first_kwargs["read_only_paths"], read_only)
        self.assertEqual(continuation_kwargs["read_only_paths"], read_only)
        self.assertNotIn("Checkpoint:", continuation_kwargs["repair_feedback"])
        self.assertIn("assertion failed", continuation_kwargs["repair_feedback"])
        checkpoint_path = next(self.runs_root.rglob("step-feature-checkpoint-1.json"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["read_only_acceptance_paths"], read_only)
        self.assertEqual(checkpoint["diagnostics"][0]["exit"], 1)
        self.assertEqual(checkpoint["read_audit"]["read_count"], 22)
        self.assertEqual(checkpoint["read_audit"]["search_count"], 19)
        self.assertEqual(checkpoint["read_audit"]["inspection_count"], 41)
        self.assertEqual(checkpoint["read_audit"]["limits"], {"reads": 46, "searches": 36, "inspections": 82})
        self.assertTrue(checkpoint["repair_capsule"]["source_excerpts_are_pre_edit"])
        self.assertIn("def render(): return 'home'", checkpoint["repair_capsule"]["first_attempt_source_excerpts"])
        self.assertIn("+# attempt 1", checkpoint["repair_capsule"]["current_scoped_diff"])
        self.assertIn("Do not read runner checkpoints", continuation_kwargs["repair_feedback"])
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
             mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["docker", "logs"], 0, "", "")):
            result = autonomous.run_child(
                self.workspace, step, "Implement the feature", requirements, "test-key", [], timeout=700,
                transcript_path=self.runs_root / "worker.jsonl", repair_feedback=repair_feedback,
                read_only_paths=["test/test_app.py"],
            )

        self.assertFalse(result["success"], "an available telemetry window with no Qwen call must not pass")
        self.assertTrue(result["child_handoff"])
        self.assertEqual(result["telemetry"]["status"], "available")
        self.assertEqual(result["qwen_route_calls"], 0)
        self.assertFalse(result["qwen_route_verified"])
        agent_spec = json.loads(command.call_args.kwargs["agents"])[autonomous.AUTONOMOUS_CHILD]["prompt"]
        self.assertIn(str((self.workspace / "test/test_app.py").resolve()), agent_spec)
        self.assertIn("Diagnostic: assertion failed", agent_spec)
        self.assertIn("Do not reread them automatically", agent_spec)
        self.assertNotIn("Read these caller-supplied acceptance and supporting reference files before editing", agent_spec)
        self.assertIn("10 Read calls", agent_spec)
        self.assertEqual(cli_call["cwd"], self.workspace)
        self.assertEqual(cli_call["timeout"], 600)

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
             mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["docker", "logs"], 0, ROUTE_LOGS, "")):
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

    def test_run_child_accepts_valid_scout_handoff_after_parent_timeout(self):
        source = self.workspace / "src" / "scout.js"
        source.write_text("function route() { return true; }\n", encoding="utf-8")
        step = {"id": "step-scout", "title": "Inspect route", "task": "Add token route",
                "files": ["src/scout.js"], "reference_paths": [], "covers": [], "checks": []}
        spec_holder = {}

        def fake_cli(command, **kwargs):
            worker = json.loads(spec_holder["agents"])[autonomous.AUTONOMOUS_CHILD]
            marker = re.search(r"append the exact marker (QWEN_AUTONOMOUS_COMPLETE_[A-Za-z0-9_-]+)",
                               worker["prompt"]).group(1)
            report = {"summary": "One route entry point.",
                      "implementation_plan": "Add the handler before the catch-all.",
                      "files": [{"path": "src/scout.js", "symbols": ["route"],
                                 "ranges": [{"offset": 1, "limit": 1, "purpose": "route definition"}]}],
                      "risks": []}
            events = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                                    "id": "child-scout"}]}},
                {"type": "assistant", "parent_tool_use_id": "child-scout", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "id": "read-scout",
                     "input": {"file_path": str(source.resolve()), "offset": 1, "limit": 1}},
                ]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read-scout",
                                                               "content": "function route() { return true; }"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "child-scout",
                                                               "content": json.dumps(report) + "\n" + marker}]}},
            ]
            write_test_result_cap_receipts(Path(kwargs["transcript_path"]), events)
            return "\n".join(json.dumps(event) for event in events), "parent timed out", 124, "deadline", True

        with mock.patch.object(autonomous.dispatch, "claude_command", side_effect=lambda **kwargs: (spec_holder.update(kwargs) or ["claude"])), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["docker", "logs"], 0, ROUTE_LOGS, "")):
            result = autonomous.run_child(
                self.workspace, step, "Implement a route", [], "test-key", [], timeout=5,
                transcript_path=self.runs_root / "scout-timeout.jsonl", phase="scout",
                inspection_limits=autonomous.SOURCE_SCOUT_LIMITS,
            )

        self.assertFalse(result["final_success"])
        self.assertTrue(result["success"])
        self.assertEqual(result["inspection_report"]["files"][0]["path"], "src/scout.js")

    def test_run_child_accepts_valid_inspection_handoff_after_parent_timeout(self):
        source = self.workspace / "src" / "inspect.js"
        source.write_text("function route() { return true; }\n", encoding="utf-8")
        step = {"id": "step-inspection", "title": "Inspect route", "task": "Add token route",
                "files": ["src/inspect.js"], "reference_paths": [], "covers": [], "checks": []}
        spec_holder = {}

        def fake_cli(command, **kwargs):
            marker = re.search(r"append the exact marker (QWEN_AUTONOMOUS_COMPLETE_[A-Za-z0-9_-]+)",
                               json.loads(spec_holder["agents"])[autonomous.AUTONOMOUS_CHILD]["prompt"])
            report = {"summary": "One route entry point.",
                      "implementation_plan": "Add the handler before the catch-all.",
                      "files": [{"path": "src/inspect.js", "symbols": ["route"],
                                 "ranges": [{"offset": 1, "limit": 1, "purpose": "route definition"}]}],
                      "risks": []}
            events = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                                    "id": "child-inspection"}]}},
                {"type": "assistant", "parent_tool_use_id": "child-inspection", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "id": "read-inspection",
                     "input": {"file_path": str(source.resolve()), "offset": 1, "limit": 1}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read-inspection",
                                                               "content": "function route() { return true; }"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "child-inspection",
                                                               "content": json.dumps(report) + "\n" + marker.group(1)}]}},
            ]
            write_test_result_cap_receipts(Path(kwargs["transcript_path"]), events)
            return "\n".join(json.dumps(event) for event in events), "parent timed out", 124, "deadline", True

        with mock.patch.object(autonomous.dispatch, "claude_command",
                               side_effect=lambda **kwargs: (spec_holder.update(kwargs) or ["claude"])), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["docker", "logs"], 0, ROUTE_LOGS, "")):
            result = autonomous.run_child(
                self.workspace, step, "Implement a route", [], "test-key", [], timeout=5,
                transcript_path=self.runs_root / "inspection-timeout.jsonl", phase="inspection",
                inspection_limits=autonomous.IMPLEMENTATION_PHASE_LIMITS,
            )

        self.assertFalse(result["final_success"])
        self.assertTrue(result["success"])
        self.assertEqual(result["inspection_report"]["files"][0]["path"], "src/inspect.js")

    def test_run_child_normalizes_custom_inspection_json_from_audited_reads(self):
        source = self.workspace / "src" / "scout.js"
        source.write_text("function route() { return true; }\n", encoding="utf-8")
        step = {"id": "step-scout", "title": "Inspect route", "task": "Add token route",
                "files": ["src/scout.js"], "reference_paths": [], "covers": [], "checks": []}
        spec_holder = {}

        def fake_cli(command, **kwargs):
            worker = json.loads(spec_holder["agents"])[autonomous.AUTONOMOUS_CHILD]
            marker = re.search(r"append the exact marker (QWEN_AUTONOMOUS_COMPLETE_[A-Za-z0-9_-]+)",
                               worker["prompt"]).group(1)
            events = [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Agent", "id": "child-scout"}]}},
                {"type": "assistant", "parent_tool_use_id": "child-scout", "message": {
                    "model": dispatch.MODEL, "content": [
                        {"type": "tool_use", "name": "Read", "id": "read-scout",
                         "input": {"file_path": str(source.resolve()), "offset": 1, "limit": 1}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "read-scout",
                     "content": "function route() { return true; }"}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "child-scout",
                     "content": '{"goal_summary":"The route is in src/scout.js.","source_notes":[]}\n' + marker}]}},
                {"type": "result", "subtype": "success", "result": "inspection complete"},
            ]
            write_test_result_cap_receipts(Path(kwargs["transcript_path"]), events)
            return "\n".join(json.dumps(event) for event in events), "", 0, None, True

        with mock.patch.object(autonomous.dispatch, "claude_command",
                               side_effect=lambda **kwargs: (spec_holder.update(kwargs) or ["claude"])), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["docker", "logs"], 0, ROUTE_LOGS, "")):
            result = autonomous.run_child(
                self.workspace, step, "Implement a route", [], "test-key", [], timeout=5,
                transcript_path=self.runs_root / "scout-custom-schema.jsonl", phase="scout",
                inspection_limits=autonomous.SOURCE_SCOUT_LIMITS,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["inspection_report_source"], "audited_read_fallback")
        self.assertEqual(result["inspection_report"]["summary"], "Audited source context for Inspect route.")
        self.assertEqual(result["inspection_report"]["files"][0]["path"], "src/scout.js")
        self.assertEqual(result["inspection_report"]["files"][0]["ranges"][0]["offset"], 1)

    def test_run_child_inspection_phase_is_read_only_and_requires_source_ranges(self):
        step = {"id": "step-feature", "title": "Persist feature", "task": "Persist the feature",
                "files": ["src/app.py"],
                "source_ranges": [{"path": "src/app.py", "offset": 1, "limit": 1,
                                   "purpose": "existing value assignment"}],
                "covers": ["req-feature"], "checks": ["Feature persists"]}
        requirements = [{"id": "req-feature", "acceptance": "The feature is persisted"}]
        spec_holder = {}

        def make_command(**kwargs):
            spec_holder.update(kwargs)
            return ["claude"]

        def fake_cli(command, **kwargs):
            worker = json.loads(spec_holder["agents"])[autonomous.AUTONOMOUS_CHILD]
            marker = re.search(r"append the exact marker (QWEN_AUTONOMOUS_COMPLETE_[A-Za-z0-9_-]+)",
                               worker["prompt"]).group(1)
            report = {"summary": "The source has a single state variable.",
                      "implementation_plan": "Update the existing state assignment.",
                      "risks": [], "files": [{"path": "src/app.py", "symbols": ["value"],
                          "ranges": [{"offset": 1, "limit": 1,
                                      "purpose": "locate the assignment"}]}]}
            events = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                                     "id": "child-inspect"}]}},
                {"type": "assistant", "parent_tool_use_id": "child-inspect", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "id": "read-1",
                     "input": {"file_path": str((self.workspace / "src/app.py").resolve()),
                               "offset": 1, "limit": 1}},
                ]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read-1",
                                                               "content": "value = 1"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "child-inspect",
                                                               "content": json.dumps(report) + "\n" + marker}]}},
                {"type": "result", "subtype": "success", "result": "inspection complete"},
            ]
            write_test_result_cap_receipts(Path(kwargs["transcript_path"]), events)
            return "\n".join(json.dumps(event) for event in events), "", 0, None, True

        before = autonomous.file_hashes(self.workspace)
        with mock.patch.object(autonomous.dispatch, "claude_command", side_effect=make_command), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded", side_effect=fake_cli), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["docker", "logs"], 0, ROUTE_LOGS, "")):
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
        self.assertIn("Prefer one or two writable files per step", prompt)
        self.assertIn("never weaken or omit acceptance", prompt)
        self.assertIn('"source_ranges": [{"path": "src/file.js", "offset": 1, "limit": 80, "purpose": "Relevant API and call pattern"}]', prompt)
        self.assertIn("source_ranges must be a JSON array", prompt)
        self.assertIn("never a map or prose labels", prompt)
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

    def test_planner_prompt_explains_exact_test_only_authorization(self):
        prompt = autonomous.planner_prompt(
            "Add only the requested regression test", [], self.workspace,
            ["src/app.py", "test/test_app.py"], [["node", "--test"]],
            max_files_per_step=1, read_only_paths=["src/app.py"],
            directories=autonomous.workspace_directories(self.workspace),
            test_only_paths=["test/test_app.py"],
        )
        self.assertIn("This caller explicitly authorized a test-only task", prompt)
        self.assertIn('"test/test_app.py"', prompt)
        self.assertIn("Do not add implementation or marker files", prompt)
        self.assertNotIn("Do not create a validation-only or test-only step", prompt)

    def test_default_plan_granularity_is_two_with_explicit_override(self):
        self.assertEqual(autonomous.DEFAULT_MAX_FILES_PER_STEP, 2)
        plan = {"steps": [{"id": "step-1", "title": "Wide", "task": "Wide task",
                           "files": ["src/app.py", "test/test_app.py", "README.md"],
                           "depends_on": [], "checks": []}]}
        with self.assertRaisesRegex(ValueError, "1 to 2 exact files"):
            autonomous.validate_plan(plan, self.workspace)
        validated = autonomous.validate_plan(plan, self.workspace, max_files_per_step=3)
        self.assertEqual(len(validated["steps"][0]["files"]), 3)

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

    def test_planner_accepts_test_only_plan_when_caller_names_exact_path(self):
        requirement = {"id": "req-regression", "acceptance": "The new regression test covers the package tender",
                       "validator_indices": [0], "step_validator_indices": [0]}
        plan_data = {"summary": "Add the requested regression test", "steps": [{
            "id": "step-regression", "title": "Add package consumption regression",
            "task": "Edit only the requested test file and add the HTTP regression scenario.",
            "files": ["test/new-feature.test.cjs"], "depends_on": [],
            "checks": ["The new regression test passes"], "covers": ["req-regression"],
        }]}
        result_event = json.dumps({"type": "result", "subtype": "success",
                                   "result": json.dumps(plan_data)})
        with mock.patch.object(autonomous.dispatch, "claude_command", return_value=["claude"]), \
             mock.patch.object(autonomous.dispatch, "cli_env", return_value={}), \
             mock.patch.object(autonomous.dispatch, "run_cli_bounded",
                               return_value=(result_event, "", 0, None, True)):
            plan, receipt = autonomous.plan_with_qwen(
                self.workspace, "Add the package redemption regression test", [requirement], "test-key",
                timeout=30, max_files_per_step=1, validators=[["node", "--test"]],
                read_only_paths=[], transcript_dir=self.runs_root,
                test_only_paths=["test/new-feature.test.cjs"],
            )

        self.assertEqual(plan["steps"][0]["files"], ["test/new-feature.test.cjs"])
        self.assertFalse(receipt["repaired"])

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

    def test_baseline_exception_is_saved_as_terminal_run_failure(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                       "validator_indices": [0], "step_validator_indices": [0]}
        with mock.patch.object(autonomous, "run_validators", side_effect=RuntimeError("validator launch failed")):
            with self.assertRaisesRegex(RuntimeError, "validator launch failed"):
                autonomous.run_request(
                    self.workspace, "Implement a feature", [["acceptance"]],
                    runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                    validator_timeout=5, requirements=[requirement],
                )

        run_dir = max(self.runs_root.glob("qwen-autonomous-*"), key=lambda item: item.stat().st_mtime)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed_exception")
        self.assertIn("validator launch failed", state["error"])
        self.assertTrue(state.get("finished_at"))
        self.assertFalse(state["workspace_dirty"])

    def test_gateway_readiness_exception_is_saved_as_terminal_run_failure(self):
        (self.workspace / ".qwen-disposable.json").write_text('{"disposable": true}', encoding="utf-8")
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                       "validator_indices": [0], "step_validator_indices": [0]}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, path):
                if path == "/health/readiness":
                    raise RuntimeError("gateway readiness unavailable")
                raise AssertionError(f"unexpected gateway request: {path}")

        with mock.patch.object(autonomous, "run_validators", return_value=[
                {"argv": ["baseline"], "exit": 0, "stdout": "passed", "stderr": ""}]), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()):
            with self.assertRaisesRegex(RuntimeError, "gateway readiness unavailable"):
                autonomous.run_request(
                    self.workspace, "Implement a feature", [["acceptance"]],
                    runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                    validator_timeout=5, requirements=[requirement],
                )

        run_dir = max(self.runs_root.glob("qwen-autonomous-*"), key=lambda item: item.stat().st_mtime)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed_exception")
        self.assertIn("gateway readiness unavailable", state["error"])
        self.assertTrue(state.get("finished_at"))
        self.assertFalse(state["workspace_dirty"])
        self.assertEqual(state["baseline_validation"][0]["exit"], 0)

    def test_workspace_run_lock_rejects_a_second_concurrent_run(self):
        ready_file = self.runs_root / "workspace-lock-ready"
        release_file = self.runs_root / "workspace-lock-release"
        child_code = """
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import autonomous
with_lock = autonomous.workspace_run_lock(Path(sys.argv[2]))
with_lock.__enter__()
Path(sys.argv[3]).write_text("LOCKED")
deadline = time.monotonic() + 5
while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
with_lock.__exit__(None, None, None)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(Path(__file__).parent), str(self.workspace),
             str(ready_file), str(release_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready_file.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_file.exists(), "child process did not acquire the workspace lock in time")
            with self.assertRaisesRegex(RuntimeError, "another Qwen run already holds"):
                with autonomous.workspace_run_lock(self.workspace):
                    self.fail("second process unexpectedly acquired the workspace lock")
        finally:
            release_file.write_text("release", encoding="utf-8")
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            stderr = child.stderr.read()
            child.stderr.close()
            ready_file.unlink(missing_ok=True)
            release_file.unlink(missing_ok=True)
        self.assertEqual(child.returncode, 0, stderr)
        with autonomous.workspace_run_lock(self.workspace):
            pass  # A finished process releases its OS lock automatically.

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

    def test_implementation_checkpoint_accepts_first_non_ejs_edit_as_progress(self):
        source = self.workspace / "src" / "booking-api-server.js"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("module.exports = {};\n", encoding="utf-8")
        receipt = self.runs_root / "progress.json"
        scope = {"root": str(self.workspace), "write_allowed": ["src/booking-api-server.js"],
                 "validators": [["node", "check-ejs.js"]]}
        payload = {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
            "tool_name": "Edit", "tool_input": {"file_path": str(source)},
            "tool_response": "The file has been updated successfully.",
        }]}
        with mock.patch.object(template_checkpoint.subprocess, "run") as validator:
            result = template_checkpoint.run_checkpoint(payload, scope, receipt)
        self.assertEqual(result["status"], "progress")
        self.assertEqual(result["paths"], ["src/booking-api-server.js"])
        validator.assert_not_called()
        self.assertTrue(dispatch.checkpoint_receipt_complete(receipt))

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
        dispatch_event = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Agent", "input": {"description": "implement"}},
        ]}})
        source = f"import time; time.sleep(1.2); print({dispatch_event!r}, flush=True); time.sleep(3)"
        started = time.monotonic()
        result = dispatch.run_cli_bounded(
            [sys.executable, "-c", source], task_input="",
            cwd=self.workspace, env=dict(os.environ), timeout=10, idle_timeout=10,
            checkpoint_receipt_path=missing_receipt, checkpoint_deadline=1,
        )
        self.assertEqual(result[3], "Qwen first-edit checkpoint was not reached within 1 seconds after worker delegation")
        self.assertGreaterEqual(time.monotonic() - started, 2.0)
        self.assertTrue(result[4])

    def test_checkpoint_watchdog_waits_for_worker_delegation(self):
        receipt = self.runs_root / "not-delegated-checkpoint.json"
        started = time.monotonic()
        result = dispatch.run_cli_bounded(
            [sys.executable, "-c", "import time; time.sleep(1.2)"], task_input="",
            cwd=self.workspace, env=dict(os.environ), timeout=5, idle_timeout=5,
            checkpoint_receipt_path=receipt, checkpoint_deadline=0.2,
        )
        self.assertIsNone(result[3])
        self.assertGreaterEqual(time.monotonic() - started, 1.0)

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

    def test_validator_rejects_missing_explicit_node_test_file_even_if_node_would_exit_zero(self):
        validator = ["node", "--test", "test/booking-addons-public.test.cjs", "test/test_app.py"]
        with mock.patch.object(autonomous.subprocess, "run") as run:
            results = autonomous.run_validators(self.workspace, [validator], timeout=10)
        self.assertEqual(results[0]["exit"], 2)
        self.assertIn("test/booking-addons-public.test.cjs", results[0]["stderr"])
        run.assert_not_called()

    def test_validator_accepts_existing_node_test_targets(self):
        validator = ["node", "--test", "test/test_app.py"]
        with mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                validator, 0, "tests passed", "")) as run:
            results = autonomous.run_validators(self.workspace, [validator], timeout=10)
        self.assertEqual(results[0]["exit"], 0)
        run.assert_called_once()

    def test_validator_checks_test_target_after_option_terminator(self):
        validator = ["node", "--test", "--", "test/missing.test.cjs"]
        with mock.patch.object(autonomous.subprocess, "run") as run:
            results = autonomous.run_validators(self.workspace, [validator], timeout=10)
        self.assertEqual(results[0]["exit"], 2)
        self.assertIn("test/missing.test.cjs", results[0]["stderr"])
        run.assert_not_called()

    def test_validator_skips_separate_test_reporter_destination_value(self):
        validator = ["node", "--test", "--test-reporter-destination", "missing-results.tap", "test/test_app.py"]
        with mock.patch.object(autonomous.subprocess, "run", return_value=subprocess.CompletedProcess(
                validator, 0, "tests passed", "")) as run:
            results = autonomous.run_validators(self.workspace, [validator], timeout=10)
        self.assertEqual(results[0]["exit"], 0)
        run.assert_called_once()

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


    # ------------------------------------------------------------------
    # Run-scoped validator TEMP/TMP environment regression tests.
    #
    # These cover the behavior change that each run gets its own copied
    # environment (TEMP/TMP pointed at that run's artifact directory) and
    # that this environment is forwarded to the validator phases.  The
    # parent os.environ must never be mutated and the forwarded value must
    # be a distinct dict copy, never os.environ itself.
    # ------------------------------------------------------------------

    def test_run_validators_forwards_explicit_env_to_subprocess(self):
        # Requirement 1a: run_validators must forward an explicitly supplied
        # env dict verbatim to subprocess.run (env=... keyword).
        env = {"TEMP": str(self.runs_root / "artifacts"),
               "TMP": str(self.runs_root / "artifacts"),
               "PATH": "/usr/bin"}
        validator = ["echo", "temp"]
        with mock.patch.object(autonomous.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   validator, 0, "", "")) as run:
            autonomous.run_validators(self.workspace, [validator],
                                      timeout=5, env=env)
        run.assert_called_once()
        self.assertIs(run.call_args.kwargs["env"], env)

    def test_run_validators_sends_none_env_when_omitted(self):
        # Requirement 1b: when no env is supplied run_validators must send
        # env=None (never os.environ) so subprocess inherits the parent env.
        validator = ["echo", "temp"]
        with mock.patch.object(autonomous.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   validator, 0, "", "")) as run:
            autonomous.run_validators(self.workspace, [validator], timeout=5)
        run.assert_called_once()
        self.assertIsNone(run.call_args.kwargs["env"])

    def test_run_request_forwards_run_scoped_env_to_validator_phases(self):
        # Requirement 2: run_request must forward ONE copied run-scoped
        # environment to the baseline, step, repair, final and final-retry
        # validator paths it exercises.  The parent os.environ must stay
        # untouched and the forwarded dict must be a copy (identity !=
        # os.environ).  The exact TEMP/TMP values are asserted in the
        # template_checkpoint tests below; here the acceptance contract is
        # that every validator phase receives a dict (never None, never
        # os.environ itself).
        (self.workspace / ".qwen-disposable.json").write_text(
            '{"disposable": true}', encoding="utf-8")
        (self.workspace / "views").mkdir()
        (self.workspace / "views" / "page.ejs").write_text("<p>before</p>\n", encoding="utf-8")
        requirement = {"id": "req-feature", "acceptance": "Feature is implemented",
                      "validator_indices": [0], "step_validator_indices": [0]}
        plan = {"summary": "One implementation step", "steps": [{
            "id": "step-feature", "title": "Implement feature", "task": "Implement it",
            "files": ["views/page.ejs"], "depends_on": [], "checks": ["Feature works"],
            "covers": ["req-feature"], "step_validator_indices": [0],
            "source_ranges": [{"path": "views/page.ejs", "offset": 1, "limit": 1,
                               "purpose": "Existing implementation entry point"}],
        }]}
        # Drive the run through the same preverified-inspection path as the
        # existing run_request fixtures: the EJS target exercises the
        # orchestrator-to-child checkpoint temp-path handoff while the child
        # and validator processes remain deterministic mocks.
        preverified = {"step-feature": {
            "summary": "Existing handler is one line in the assigned file.",
            "implementation_plan": "Preserve its current behavior and add the feature in the same module.",
            "risks": [],
            "files": [{"path": "views/page.ejs", "symbols": ["template"],
                       "ranges": [{"offset": 1, "limit": 1,
                                   "purpose": "Existing implementation entry point"}]}],
        }}

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

        validator_calls = []

        def record_validators(workspace, validators, **kwargs):
            validator_calls.append((validators, kwargs.get("env")))
            return [{"argv": list(argv), "exit": 0, "stdout": "passed", "stderr": ""}
                    for argv in validators]

        child_calls = []

        def child(workspace, step, request, requirements, key, previous, **kwargs):
            phase = kwargs.get("phase")
            child_calls.append(kwargs)
            if phase == "implementation":
                (self.workspace / "views" / "page.ejs").write_text(
                    "<p>feature</p>\n", encoding="utf-8")
            return {"phase": phase, "success": True, "exit": 0, "stop_reason": None,
                    "seconds": 1, "child_tool_calls": ["Edit"], "qwen_route_calls": 1,
                    "child_handoff": True, "child_result": "done", "final": "complete",
                    "early_template_checkpoint": {"status": "passed"},
                    "read_audit": {"read_count": 0, "search_count": 0,
                                   "inspection_count": 0,
                                   "counts_by_tool": {"Read": 0, "Grep": 0, "Glob": 0},
                                   "reads": [], "violations": [], "within_budget": True}}

        parent_snapshot = {k: v for k, v in os.environ.items()}
        passed = [{"argv": ["acceptance"], "exit": 0, "stdout": "passed", "stderr": ""}]
        with mock.patch.object(autonomous, "run_validators",
                               side_effect=record_validators), \
             mock.patch.object(autonomous.httpx, "Client", return_value=Client()), \
             mock.patch.object(autonomous.dispatch, "gateway_key", return_value="test-key"), \
             mock.patch.object(autonomous.dispatch, "set_policy"), \
             mock.patch.object(autonomous, "run_child", side_effect=child):
            result = autonomous.run_request(
                self.workspace, "Implement a feature", [["acceptance"]],
                runs_root=self.runs_root, total_timeout=30, step_timeout=5,
                validator_timeout=5, baseline_validators=[["baseline"]],
                requirements=[requirement], plan_override=plan,
                preverified_inspections=preverified,
                checkpoint_validators=[["syntax"]])

        # Parent environment must be byte-for-byte unchanged.
        self.assertEqual(os.environ, parent_snapshot)

        # The happy path exercises baseline, step, and final validation.
        # All three must receive the same copied environment, and TEMP/TMP
        # must live under this run's artifact directory.
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(child_calls), 1)
        self.assertEqual(child_calls[0]["checkpoint_validators"], [["syntax"]])
        self.assertEqual(Path(child_calls[0]["checkpoint_temp_dir"]).resolve(),
                         Path(validator_calls[0][1]["TEMP"]).resolve())
        self.assertEqual(len(validator_calls), 3)
        self.assertEqual([validators for validators, _env in validator_calls],
                         [[["baseline"]], [["acceptance"]], [["acceptance"]]])
        envs = [env for _validators, env in validator_calls]
        self.assertTrue(all(isinstance(env, dict) for env in envs))
        self.assertTrue(all(env is envs[0] for env in envs))
        self.assertIsNot(envs[0], os.environ)
        self.assertEqual(envs[0]["TEMP"], envs[0]["TMP"])
        validator_temp = Path(envs[0]["TEMP"]).resolve()
        self.assertEqual(validator_temp.name, "validator-temp")
        self.assertTrue(validator_temp.is_dir())
        self.assertEqual(validator_temp.parent.parent, self.runs_root.resolve())

    def test_worker_tool_settings_checkpoint_json_only_serializes_temp_dir(self):
        # Requirement 3: worker_tool_settings must serialize ONLY the
        # checkpoint temp directory into the saved checkpoint JSON.  It must
        # never serialize the full copied environment or any credential.
        step = {"id": "step-ui", "files": ["views/page.ejs"], "reference_paths": []}
        temp_dir = str(self.runs_root / "artifacts")
        secret_value = "super-secret-token"
        settings_path = self.runs_root / "worker.jsonl"
        autonomous.worker_tool_settings(
            self.workspace, step, [], "implementation", settings_path,
            checkpoint_validators=[["node", "scripts/check-ejs.js"]],
            checkpoint_temp_dir=temp_dir)
        checkpoint_path = self.runs_root / "worker.checkpoint.json"
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        # The checkpoint temp directory must be present.
        self.assertIn("checkpoint_temp_dir", payload)
        self.assertEqual(payload["checkpoint_temp_dir"], temp_dir)
        # No environment mapping may be serialized.
        self.assertNotIn("env", payload)
        self.assertNotIn("environment", payload)
        self.assertNotIn("TEMP", payload)
        self.assertNotIn("TMP", payload)
        # No credential value may be serialized anywhere in the checkpoint.
        self.assertNotIn(secret_value, json.dumps(payload))
        self.assertNotIn("token", json.dumps(payload).lower())

    def test_template_checkpoint_forwards_configured_temp_env_to_subprocess(self):
        # Requirement 4a: when a checkpoint temp directory is configured,
        # template_checkpoint.run_checkpoint must forward a copied env whose
        # TEMP/TMP point at that directory to the validator subprocess.
        template = self.workspace / "views" / "page.ejs"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("<p>ok</p>\n", encoding="utf-8")
        receipt = self.runs_root / "receipt.json"
        scope = {"root": str(self.workspace), "write_allowed": ["views/page.ejs"],
                 "validators": [["node", "scripts/check-ejs.js"]],
                 "checkpoint_temp_dir": str(self.runs_root / "artifacts")}
        payload = {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
            "tool_name": "Edit", "tool_input": {"file_path": str(template)},
            "tool_response": "The file has been updated successfully."}]}
        parent_snapshot = {k: v for k, v in os.environ.items()}
        with mock.patch.object(template_checkpoint.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, "EJS syntax valid", "")) as run:
            result = template_checkpoint.run_checkpoint(payload, scope, receipt)
        self.assertEqual(result["status"], "passed")
        run.assert_called_once()
        forwarded = run.call_args.kwargs.get("env")
        self.assertIsNotNone(forwarded, "configured temp dir must forward an env")
        self.assertIsNot(forwarded, os.environ)
        self.assertEqual(forwarded["TEMP"], scope["checkpoint_temp_dir"])
        self.assertEqual(forwarded["TMP"], scope["checkpoint_temp_dir"])
        # Parent env must be unchanged.
        self.assertEqual(os.environ, parent_snapshot)

    def test_template_checkpoint_preserves_none_env_when_no_temp_configured(self):
        # Requirement 4b: when no checkpoint temp directory is configured,
        # template_checkpoint.run_checkpoint must forward env=None (so the
        # parent env is inherited) rather than a fabricated environment.
        template = self.workspace / "views" / "page.ejs"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("<p>ok</p>\n", encoding="utf-8")
        receipt = self.runs_root / "receipt.json"
        scope = {"root": str(self.workspace), "write_allowed": ["views/page.ejs"],
                 "validators": [["node", "scripts/check-ejs.js"]]}
        payload = {"agent_type": "qwen-autonomous-implementer", "tool_calls": [{
            "tool_name": "Edit", "tool_input": {"file_path": str(template)},
            "tool_response": "The file has been updated successfully."}]}
        with mock.patch.object(template_checkpoint.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, "EJS syntax valid", "")) as run:
            result = template_checkpoint.run_checkpoint(payload, scope, receipt)
        self.assertEqual(result["status"], "passed")
        run.assert_called_once()
        self.assertIsNone(run.call_args.kwargs.get("env"))


if __name__ == "__main__":
    unittest.main()
