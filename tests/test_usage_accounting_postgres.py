"""Opt-in isolated PostgreSQL fixture, never the serving proxy database.

SUBROUTE_TEST_POSTGRES_URL must point to a disposable database. The fixture
uses a unique temporary schema and drops it afterward; no provider calls.
"""
import asyncio
import json
import os
import re
import uuid
import sys
import selectors

import pytest

from subroute.usage_accounting import ReceiptStore, Trace


pytestmark = pytest.mark.skipif(not os.getenv("SUBROUTE_TEST_POSTGRES_URL"), reason="Isolated PostgreSQL fixture not configured")


class PgAdapter:
    def __init__(self, connection):
        self.connection = connection

    def sql(self, query, args):
        values = []
        def replace(match):
            values.append(args[int(match.group(1))-1])
            return "%s"
        return re.sub(r"\$(\d+)", replace, query), values

    async def execute_raw(self, query, *args):
        query, values = self.sql(query, args)
        cursor = await self.connection.execute(query, values)
        return cursor.rowcount

    async def query_raw(self, query, *args):
        query, values = self.sql(query, args)
        cursor = await self.connection.execute(query, values)
        return await cursor.fetchall()


def test_actual_postgres_outage_restart_revision_ack_and_environment():
    import psycopg
    from psycopg.rows import dict_row
    async def scenario():
        url = os.environ["SUBROUTE_TEST_POSTGRES_URL"]
        # Keep this test opt-in and visibly isolated even if misconfigured.
        assert "accounting-fixture-only" in url, "Use the documented disposable PostgreSQL fixture"
        connection = await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row)
        schema = "receipt_test_" + uuid.uuid4().hex
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await connection.execute(f'SET search_path TO "{schema}"')
            store = ReceiptStore(PgAdapter(connection))
            trace = Trace(str(uuid.uuid4()), "staging", "key-1", "customer-1", str(uuid.uuid4()), "/v1/messages", "advisor")
            ident = await store.start(trace, "gpt-6-luna", "codex-subscription", "advisor")
            other = await store.start(Trace(str(uuid.uuid4()), "production"), "minimax", "openrouter", "inference")
            await store.update(ident, {"input_tokens": 10})
            first = (await store.pending("staging"))[0]
            assert first["revision"] == 2 and first["input_tokens"] == 10
            assert "output_tokens" not in first and "provider_cost_micros" not in first
            # Restart the adapter; durable facts and unacknowledged revisions remain.
            restarted = ReceiptStore(PgAdapter(connection))
            assert (await restarted.pending("staging"))[0] == first
            await restarted.update(ident, {"output_tokens": 3, "usage_complete": True}, "complete")
            # Stale or duplicate acknowledgements cannot swallow a newer revision.
            await restarted.ack("staging", [{"attempt_id": ident, "revision": 2}])
            await restarted.ack("staging", [{"attempt_id": ident, "revision": 2}])
            terminal = (await restarted.pending("staging"))[0]
            assert terminal["revision"] == 3 and terminal["outcome"] == "complete"
            await restarted.ack("production", [{"attempt_id": ident, "revision": 3}])
            assert (await restarted.pending("staging"))[0]["attempt_id"] == ident
            await restarted.ack("staging", [{"attempt_id": ident, "revision": 4}])
            assert len(await restarted.pending("staging")) == 1
            await restarted.ack("staging", [{"attempt_id": ident, "revision": 3}])
            assert await restarted.pending("staging") == []
            assert (await restarted.pending("production"))[0]["attempt_id"] == other
            # A dead connection cannot acknowledge receipts, so the next consumer retries.
            await connection.close()
            assert await restarted.update(other, {"input_tokens": 12}, "complete") is False
            new_connection = await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row)
            connection = new_connection
            await connection.execute(f'SET search_path TO "{schema}"')
            recovered = ReceiptStore(PgAdapter(connection))
            assert (await recovered.pending("production"))[0]["outcome"] == "pending"
            # Stale interrupted receipts retain partial numbers; late facts do not revive outcome.
            await recovered.update(other, {"input_tokens": 12})
            await connection.execute("UPDATE subroute_usage_receipts SET updated_at=0 WHERE attempt_id=%s", (other,))
            assert (await recovered.pending("production"))[0]["outcome"] == "interrupted"
            await recovered.update(other, {"output_tokens": 4, "usage_complete": True}, "complete")
            late = (await recovered.pending("production"))[0]
            assert late["outcome"] == "interrupted" and late["output_tokens"] == 4 and late["usage_complete"]
            assert late["input_tokens"] == 12
        finally:
            if connection.closed:
                connection = await psycopg.AsyncConnection.connect(url, autocommit=True)
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await connection.close()
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())) as runner:
            runner.run(scenario())
    else:
        asyncio.run(scenario())
