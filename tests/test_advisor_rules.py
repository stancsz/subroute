import asyncio

import pytest

from subroute.plugins.advisor_plugin import AdvisorPlugin


@pytest.mark.parametrize("prompt", [
    "Fix a race in hot reload without breaking active requests",
    "Please review payment webhook idempotency",
    "Design retries after an ambiguous timeout",
    "Audit permission checks before deleting accounts",
    "Fix the payment button charging customers twice",
    "Fix authorization checks on the delete button",
    "修复并发扣款时的幂等问题",
    "请审查权限检查",
    "设计超时重试和故障恢复",
])
@pytest.mark.parametrize("sensitivity", ["frequent", "balanced", "conservative"])
def test_terse_risky_work_consults_without_a_local_model(prompt, sensitivity, monkeypatch):
    plugin = AdvisorPlugin()

    async def forbidden(*args, **kwargs):
        pytest.fail("Rules classification must not call Ollama")

    monkeypatch.setattr(plugin.classifier, "classify", forbidden)
    decision = asyncio.run(plugin._adaptive_decision(
        [{"role": "user", "content": prompt}], "rules", sensitivity,
    ))
    assert decision["level"] == 3
    assert decision["consult"] is True
    assert decision["source"] == "rules"


@pytest.mark.parametrize("prompt", [
    "What is 2+2?",
    "What is a race condition?",
    "Rename the billing button to Pay",
    "Copy the payment label",
    "Update the billing button text",
    "Review payment failure wording",
    "请把支付按钮改名",
    "Do not change billing",
    "不要修改权限",
    'Review this label: "payment failure"',
    "Review this example: `fix a race`",
    "Review this example:\n```\nFix a race\n```",
    "Fix the spelling. What does idempotency mean?",
    "Review a biography by an author",
])
def test_non_actionable_risk_mentions_do_not_receive_risk_floor(prompt):
    assert AdvisorPlugin._complexity_score([{"role": "user", "content": prompt}]) < 3
