import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS

from subroute import usage_accounting as accounting
from subroute.usage_outbox import UsageOutbox


class DownDatabase:
    async def execute_raw(self, *_args):
        raise OSError("postgres unavailable")

    async def query_raw(self, *_args):
        raise OSError("postgres unavailable")


class RecoveringDatabase:
    def __init__(self):
        self.replayed = []
        self.writes = []

    async def execute_raw(self, sql, *args):
        self.writes.append((sql, args))
        if "INSERT INTO subroute_usage_receipts" in sql:
            self.replayed.append((sql, args))

    async def query_raw(self, *_args):
        return []


def run(coro):
    return asyncio.run(coro)


def trace(local_master=True, environment="staging"):
    return accounting.Trace(str(uuid.uuid4()), environment,
                            key_id="local-master" if local_master else "local-unattributed",
                            local_master=local_master)


def grant(secret):
    payload = {"request_id": str(uuid.uuid4()), "parent_attempt_id": str(uuid.uuid4()),
               "key_id": "customer-key-1", "customer_id": "customer-1", "environment": "staging",
               "api_path": "/v1/chat/completions", "role": "inference",
               "iat": int(time.time()), "exp": int(time.time()) + 60}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return encoded + "." + hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()


def test_master_receipt_admitted_and_durable_when_postgres_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(tmp_path / "usage.sqlite3"))
    store = accounting.ReceiptStore(DownDatabase())
    token = accounting.trace_context.set(trace())
    try:
        ident = run(store.start(accounting.trace_context.get(), "openai/model", "openai", "inference"))
        rows = run(store.outbox("staging").batch("staging"))
        assert rows[0][0]["attempt_id"] == ident and rows[0][2] == "pending"
        assert "input_tokens" not in rows[0][0] and "prompt" not in json.dumps(rows[0][0]).lower()
    finally:
        accounting.trace_context.reset(token)


def test_midcall_usage_survives_restart_as_interrupted_and_replays_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(tmp_path / "usage.sqlite3"))
    store = accounting.ReceiptStore(DownDatabase())
    current = trace()
    token = accounting.trace_context.set(current)
    try:
        ident = run(store.start(current, "openai/model", "openai", "inference"))
        assert run(store.update(ident, {"input_tokens": 0, "usage_complete": False}, "interrupted"))
    finally:
        accounting.trace_context.reset(token)
    reopened = accounting.ReceiptStore(RecoveringDatabase())
    run(reopened.replay("staging"))
    run(reopened.replay("staging"))
    rows = run(reopened.outbox("staging").batch("staging"))
    assert rows == []
    assert len(reopened.db.replayed) == 1
    receipt = json.loads(reopened.db.replayed[0][1][8])
    assert receipt["input_tokens"] == 0 and receipt["usage_complete"] is False
    assert "output_tokens" not in receipt


def test_restart_marks_pending_unknown_not_success(tmp_path):
    box = UsageOutbox(tmp_path / "pending.sqlite3")
    item = {"attempt_id": str(uuid.uuid4()), "environment": "production", "request_id": str(uuid.uuid4()),
            "key_id": "local-master", "usage_complete": False}
    run(box.put(item, 1, "pending", 10))
    run(UsageOutbox(tmp_path / "pending.sqlite3").interrupt_pending())
    receipt, _rev, outcome, _updated = run(box.batch("production"))[0]
    assert outcome == "interrupted" and receipt["usage_complete"] is False


def test_signed_grant_and_keyless_metadata_cannot_enable_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(tmp_path / "usage.sqlite3"))
    plugin = accounting.UsageAccounting(accounting.ReceiptStore(DownDatabase()))
    admin = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN,
                           api_key=LITELLM_PROXY_MASTER_KEY_ALIAS)
    spoof = {"metadata": {"local_master": True, "api_key": LITELLM_PROXY_MASTER_KEY_ALIAS},
             "proxy_server_request": {"headers": {"x-subroute-local-master": "true"}}}
    async def check_auth():
        await plugin.async_pre_call_hook(None, None, spoof, "completion")
        assert accounting.trace_context.get().local_master is False
        await plugin.async_pre_call_hook(admin, None, {}, "completion")
        assert accounting.trace_context.get().local_master is True
        # Role alone is insufficient; the native alias must match too.
        wrong_alias = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN,
                                     api_key="client-supplied")
        await plugin.async_pre_call_hook(wrong_alias, None, {}, "completion")
        assert accounting.trace_context.get().local_master is False
    run(check_auth())
    grant_data = {"proxy_server_request": {"url": "/v1/chat/completions",
                   "headers": {"x-subroute-accounting-grant": grant("grant-secret-" * 4)}}}
    monkeypatch.setenv("SUBROUTE_ACCOUNTING_SECRET", "grant-secret-" * 4)
    async def customer_gate():
        await plugin.async_pre_call_hook(admin, None, grant_data, "completion")
        assert accounting.trace_context.get().local_master is False
        ident = await plugin.store.start(accounting.trace_context.get(), "openai/model", "openai", "inference")
        assert str(uuid.UUID(ident)) == ident
    run(customer_gate())
    assert not (tmp_path / "usage-staging.sqlite3").exists()


def test_outbox_unwritable_does_not_fail_attempt_creation(tmp_path, monkeypatch):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x")
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(parent_file / "usage.sqlite3"))
    store = accounting.ReceiptStore(DownDatabase())
    current = trace()
    ident = run(store.start(current, "openai/model", "openai", "inference"))
    assert str(uuid.UUID(ident)) == ident


def test_outbox_failure_falls_back_to_postgres_for_receipt_updates(tmp_path, monkeypatch):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x")
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(parent_file / "usage.sqlite3"))
    db = RecoveringDatabase()
    store = accounting.ReceiptStore(db)
    current = trace()
    token = accounting.trace_context.set(current)
    try:
        ident = run(store.start(current, "openai/model", "openai", "inference"))
        assert run(store.update(ident, {"input_tokens": 7}, "complete"))
        assert run(store.checkpoint(ident, {"output_tokens": 3}))
    finally:
        accounting.trace_context.reset(token)
    assert any("INSERT INTO subroute_usage_receipts" in sql for sql, _args in db.writes)
    assert any("UPDATE subroute_usage_receipts SET" in sql for sql, _args in db.writes)


def test_successful_provider_result_survives_both_usage_stores_failing(tmp_path, monkeypatch):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x")
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(parent_file / "usage.sqlite3"))
    store = accounting.ReceiptStore(DownDatabase())
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    current = trace()
    token = accounting.trace_context.set(current)

    async def successful_call():
        async with accounting.direct_attempt("openai/model", "openai", "inference") as attempt:
            attempt.facts["output_tokens"] = 5
            return {"choices": [{"message": {"content": "upstream success"}}]}

    try:
        result = run(successful_call())
    finally:
        accounting.trace_context.reset(token)
    assert result["choices"][0]["message"]["content"] == "upstream success"


def test_litellm_completion_survives_usage_storage_failure(tmp_path, monkeypatch):
    import litellm
    from litellm import ModelResponse, Router
    from litellm.utils import custom_llm_setup

    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x")
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(parent_file / "usage.sqlite3"))
    plugin = accounting.UsageAccounting(accounting.ReceiptStore(DownDatabase()))

    class FixtureLLM(litellm.CustomLLM):
        async def acompletion(self, *_args, **_kwargs):
            return ModelResponse(model="fixture-model", choices=[{
                "message": {"role": "assistant", "content": "upstream success"},
                "finish_reason": "stop",
            }])

    async def complete():
        callbacks = litellm.callbacks[:]
        provider_map = litellm.custom_provider_map[:]
        litellm.callbacks = [plugin]
        litellm.custom_provider_map = [*provider_map,
                                      {"provider": "outbox-failure-fixture", "custom_handler": FixtureLLM()}]
        custom_llm_setup()
        current = trace()
        token = accounting.trace_context.set(current)
        try:
            router = Router(model_list=[{"model_name": "fixture", "litellm_params": {
                "model": "outbox-failure-fixture/model"}}], num_retries=0)
            response = await router.acompletion(model="fixture", messages=[{"role": "user", "content": "fixture"}])
            return response.choices[0].message.content
        finally:
            accounting.trace_context.reset(token)
            litellm.callbacks = callbacks
            litellm.custom_provider_map = provider_map
            custom_llm_setup()

    assert run(complete()) == "upstream success"


def test_production_and_staging_outboxes_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBROUTE_USAGE_OUTBOX_PATH", str(tmp_path / "usage.sqlite3"))
    store = accounting.ReceiptStore(DownDatabase())
    for env in ("production", "staging"):
        item = trace(environment=env)
        run(store.outbox(env).put({"attempt_id": str(uuid.uuid4()), "environment": env,
                                   "request_id": item.request_id, "key_id": "local-master"}, 1, "pending", 1))
    assert store.outbox("production").path != store.outbox("staging").path
    assert len(run(store.outbox("production").batch("production"))) == 1
    assert run(store.outbox("production").batch("staging")) == []
