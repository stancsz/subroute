"""Recovery accounting must retain failures and never turn into unbounded retry."""

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("music_recovery_experiment",
    Path(__file__).resolve().parents[1] / "scripts/verify_music_mcp.py")
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)
observe_case, summarize = experiment.observe_case, experiment.summarize


def result(status, tokens, *, code=None, inputs=None):
    body = {"status": status, "usage": {"total_tokens": tokens} if tokens is not None else None,
            "inputs": [{"sha256": "fixed"}] if inputs is None else inputs}
    if code:
        body["error"] = {"code": code}
    return SimpleNamespace(isError=status != "complete", structuredContent=body, content=[])


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def call_tool(self, tool, arguments, **kwargs):
        self.calls.append((tool, copy.deepcopy(arguments)))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


CASE = {"case": "normal_audio", "tool": "compare_audio", "arguments": {
    "a_path": "a.wav", "b_path": "b.wav", "question": "Listen without changing the task.", "focus": ["clarity"]}}


@pytest.mark.parametrize("outcome", ["complete", "refused"])
def test_one_explicit_recheck_retains_first_failure_and_both_usage(outcome):
    session = Session([result("refused", 4, code="provider_content_filter"),
                       result(outcome, 6, code="provider_content_filter" if outcome == "refused" else None)])
    saved = []
    row = asyncio.run(observe_case(session, CASE, True, lambda attempt: saved.append(attempt)))
    assert len(session.calls) == 2 and session.calls[0] == session.calls[1]
    assert len(saved) == 2 and saved[0]["is_error"]
    assert row["attempts"][0]["structured_content"]["status"] == "refused"
    assert row["recheck_inputs_match"]
    row["expected_outcome_matches"] = not row["is_error"]
    summary = summarize([row])
    assert summary["calls"] == 2 and summary["tasks"] == 1 and summary["known_provider_tokens"] == 10
    assert summary["initial_failures"] == 1
    assert summary["recovered_tasks"] == (outcome == "complete")


@pytest.mark.parametrize("first,enabled", [
    (result("complete", 6), True),
    (result("refused", 4, code="provider_content_filter"), False),
    (result("error", None, code="invalid_input"), True),
    (RuntimeError("session lost"), True),
])
def test_success_local_error_client_error_and_default_never_recheck(first, enabled):
    session = Session([first])
    row = asyncio.run(observe_case(session, CASE, enabled))
    assert len(session.calls) == 1
    row["expected_outcome_matches"] = not row["is_error"]
    summary = summarize([row])
    assert summary["calls"] == 1 and summary["conditional_recovery_rate"] == (None if not row["is_error"] else 0.0)


def test_changed_input_receipt_is_not_counted_as_recovery():
    session = Session([result("refused", None, code="provider_content_filter"),
                       result("complete", 6, inputs=[{"sha256": "changed"}])])
    row = asyncio.run(observe_case(session, CASE, True))
    row["expected_outcome_matches"] = False
    summary = summarize([row])
    assert row["is_error"] and not row["recheck_inputs_match"]
    assert summary["final_complete"] == 0 and summary["recovered_tasks"] == 0
    assert summary["provider_calls_with_unknown_usage"] == 1


def test_expected_refusal_is_not_rechecked():
    session = Session([result("refused", 4, code="provider_content_filter")])
    asyncio.run(observe_case(session, {**CASE, "expected_error": "provider_content_filter"}, True))
    assert len(session.calls) == 1


def test_expected_local_rejection_is_separate_from_eligible_success_rate():
    rows = [{"is_error": True, "structured_content": {"status": "error", "error": {"code": "invalid_input"}},
             "elapsed_seconds": 0.001, "expected_outcome_matches": True, "expected_error": "invalid_input"},
            {"is_error": False, "structured_content": {"status": "complete", "usage": {"total_tokens": 6}},
             "elapsed_seconds": 1, "expected_outcome_matches": True}]
    summary = summarize(rows)
    assert summary["tasks"] == 2 and summary["eligible_tasks"] == 1
    assert summary["observed_final_success_rate"] == 1 and summary["initial_failures"] == 0
    assert summary["conditional_recovery_rate"] is None
    assert summary["errors"] == 1 and summary["provider_calls_with_unknown_usage"] == 0
