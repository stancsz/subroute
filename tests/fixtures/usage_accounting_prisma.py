"""Run with the deployed generated Prisma client and a disposable PostgreSQL.

Not a live provider test. It leaves serving databases and processes untouched.
See docs/operations/usage-accounting.md for the fixture command.
"""
import asyncio
import json
import os
import uuid
from importlib.metadata import version

from prisma import Prisma
import litellm
from litellm import Router, Usage
from litellm.types.utils import GenericStreamingChunk, ModelResponse
from subroute import usage_accounting as accounting


async def main():
    url = os.environ["SUBROUTE_TEST_POSTGRES_URL"]
    assert "accounting-fixture-only" in url
    schema = "receipt_fixture_" + uuid.uuid4().hex
    admin = Prisma(datasource={"url": url})
    await admin.connect()
    db = None
    result = {}
    try:
        await admin.execute_raw(f'CREATE SCHEMA "{schema}"')
        db = Prisma(datasource={"url": url + "?schema=" + schema})
        await db.connect()
        assert (await db.query_raw("SELECT current_schema() AS name"))[0]["name"] == schema
        store = accounting.ReceiptStore(db)
        accounting.usage_accounting.store = store
        ident = await store.start(accounting.Trace(str(uuid.uuid4()), "staging"), "fixture-model", "fixture", "advisor")
        first = (await store.pending("staging"))[0]
        assert isinstance(first, dict) and first["attempt_id"] == ident and first["revision"] == 1
        await store.update(ident, {"input_tokens": 7, "output_tokens": 2, "usage_complete": True}, "complete")
        restarted = accounting.ReceiptStore(db)
        await restarted.ack("staging", [{"attempt_id": ident, "revision": 1}])
        terminal = (await restarted.pending("staging"))[0]
        assert terminal["outcome"] == "complete" and terminal["revision"] == 2
        # Reproduce the framework's blocking worker-loop success hook with
        # the actual Prisma store. Stream observers already saved this row.
        from litellm.litellm_core_utils.asyncify import run_async_function
        run_async_function(
            accounting.usage_accounting.async_post_call_success_deployment_hook,
            {accounting.TAG: ident, "_subroute_stream_observed": True},
            {"usage": {"input_tokens": 999}}, "aresponses",
        )
        preserved = (await store.pending("staging"))[0]
        assert preserved["revision"] == 2 and preserved["input_tokens"] == 7
        result["prisma_worker_hook_preserves_observer_receipt"] = True
        await restarted.ack("production", [{"attempt_id": ident, "revision": 2}])
        assert len(await restarted.pending("staging")) == 1
        await restarted.ack("staging", [{"attempt_id": ident, "revision": 2}])
        assert await restarted.pending("staging") == []
        result["prisma_revision_restart_ack_environment"] = True

        class FixtureLLM(litellm.CustomLLM):
            async def acompletion(self, *args, **kwargs):
                return ModelResponse(model="fixture-model", choices=[{"message": {"role": "assistant", "content": "fixture"}}],
                                     usage=Usage(prompt_tokens=17, completion_tokens=4, total_tokens=21))

            async def astreaming(self, *args, **kwargs):
                model = kwargs.get("model", "")
                raw = None if "missing" in model else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0} if "zero" in model else {"prompt_tokens": 18, "completion_tokens": 5, "total_tokens": 23}
                yield GenericStreamingChunk(text="fixture", is_finished=False, finish_reason=None, usage=None, index=0)
                yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop", usage=raw, index=0)

        litellm.callbacks = [accounting.usage_accounting]
        os.environ["SUBROUTE_ENVIRONMENT"] = "production"
        litellm.custom_provider_map = [{"provider": "receipt-fixture", "custom_handler": FixtureLLM()}]
        from litellm.utils import custom_llm_setup
        custom_llm_setup()
        accounting.install_stream_accounting()
        router = Router(model_list=[{"model_name": model, "litellm_params": {"model": "receipt-fixture/"+model}}
                                    for model in ("known", "missing", "zero")], num_retries=0)
        await accounting.usage_accounting.async_pre_call_hook({}, None, {}, "completion")
        for model, expected in (("known", 18), ("missing", None), ("zero", 0)):
            response = await router.acompletion(model=model, messages=[{"role": "user", "content": "fixture"}], stream=True)
            async for _chunk in response:
                pass
            await asyncio.sleep(0.3)
            rows = await store.pending("production")
            row = next(row for row in rows if row["upstream_model"].endswith("/" + model))
            assert row["outcome"] == "complete", row["outcome"]
            assert row.get("input_tokens") == expected, (model, row.get("input_tokens"))
            assert row["usage_complete"] == (expected is not None)
            assert "provider_cost_micros" not in row
            result["framework_stream_" + model] = True
    finally:
        if db is not None:
            await db.disconnect()
        await admin.execute_raw(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.disconnect()
    print(json.dumps({"litellm_version": version("litellm"), "checks": result, "fixture_schema_removed": True}))


asyncio.run(main())
