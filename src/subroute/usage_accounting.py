"""Numeric provider receipts and a revisioned outbox in the existing proxy database.

No provider bodies, tokens, caller metadata or price-table costs are persisted.
The deployment pre-hook runs before the framework's cache check, so a pending
receipt is a planned attempt, not proof that a provider received the request.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger(__name__)
ROLES = {"inference", "advisor", "compaction", "image"}
SAFE_LABEL = re.compile(r"[a-zA-Z0-9_.:/-]{1,160}\Z")
TAG = "_subroute_attempt_id"
LOCAL_INFERENCE_PATHS = {"chat/completions", "responses", "messages", "images/generations", "images/edits", "images/variations",
                         "completions", "embeddings", "audio/speech", "audio/transcriptions", "audio/translations", "moderations", "rerank"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS subroute_usage_receipts (
 attempt_id UUID PRIMARY KEY, environment TEXT NOT NULL,
 request_id UUID NOT NULL, parent_attempt_id UUID,
 customer_id TEXT, key_id TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('pending','complete','failed','cancelled','interrupted','cached')),
 revision INTEGER NOT NULL DEFAULT 1, acked_revision INTEGER NOT NULL DEFAULT 0,
 payload JSONB NOT NULL, updated_at BIGINT NOT NULL
)
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def number(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def label(value: Any) -> str | None:
    # A model is supplied by a provider/configuration, not an arbitrary telemetry string.
    if not isinstance(value, str) or not SAFE_LABEL.fullmatch(value) or value.startswith(("sk-", "eyJ")):
        return None
    if any(len(secret) >= 8 and secret in value for name, secret in os.environ.items()
           if name.endswith(("KEY", "TOKEN", "SECRET", "PASSWORD"))):
        return None
    return value


def identity(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and not any(ord(char) < 32 or ord(char) == 127 for char in value)


def as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return {}


def usage_facts(value: Any) -> dict:
    obj = as_dict(value)
    raw = as_dict(obj.get("usage"))
    if not raw and isinstance(obj.get("response"), dict):
        raw = as_dict(obj["response"].get("usage"))
    if not raw and isinstance(obj.get("message"), dict):
        raw = as_dict(obj["message"].get("usage"))
    fields = {
        "input_tokens": number(raw.get("input_tokens", raw.get("prompt_tokens"))),
        "output_tokens": number(raw.get("output_tokens", raw.get("completion_tokens"))),
        "total_tokens": number(raw.get("total_tokens")),
        "reasoning_tokens": number(as_dict(raw.get("output_tokens_details", raw.get("completion_tokens_details"))).get("reasoning_tokens")),
        "cached_input_tokens": number(raw.get("cache_read_input_tokens", as_dict(raw.get("input_tokens_details", raw.get("prompt_tokens_details"))).get("cached_tokens"))),
        "cache_creation_tokens": number(raw.get("cache_creation_input_tokens")),
    }
    # Only an explicitly reported provider cost, never LiteLLM response_cost.
    cost = raw.get("cost")
    if type(cost) in (int, float) and 0 <= cost <= 1_000_000:
        fields["provider_cost_micros"] = round(cost * 1_000_000)
    return {key: val for key, val in fields.items() if val is not None}


def response_outcome(value: Any) -> str:
    obj = as_dict(value)
    status = as_dict(obj.get("response")).get("status", obj.get("status"))
    return "interrupted" if status == "incomplete" else "failed" if status == "failed" else "complete"


def deployment_kind(kwargs: Mapping, call_type: Any) -> str:
    """Use LiteLLM's own provider decisions to identify protocol adapters."""
    from litellm.utils import ProviderConfigManager
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
    from litellm.types.utils import LlmProviders
    call = getattr(call_type, "value", call_type)
    model, provider, _, _ = get_llm_provider(model=str(kwargs.get("model") or ""),
                                            custom_llm_provider=kwargs.get("custom_llm_provider"), api_base=kwargs.get("api_base"))
    if call in {"anthropic_messages", "aanthropic_messages"}:
        from litellm.llms.anthropic.pass_through.messages.handler import _deployment_passes_through_anthropic_messages
        native = ProviderConfigManager.get_provider_anthropic_messages_config(model, LlmProviders(provider)) if provider in {item.value for item in LlmProviders} else None
        return "provider" if native is not None or _deployment_passes_through_anthropic_messages(kwargs.get("model_info")) else "bridge"
    if call in {"responses", "aresponses"}:
        from litellm.responses.main import _will_bridge_to_chat_completions
        # This helper recognizes the full openai/chat_completions/ prefix before
        # resolving the provider config. get_llm_provider strips that prefix.
        return "bridge" if _will_bridge_to_chat_completions(str(kwargs.get("model") or ""), provider,
            bool(kwargs.get("use_chat_completions_api")), kwargs.get("model_info"), kwargs.get("api_base")) else "provider"
    if call in {"completion", "acompletion"}:
        from litellm.main import responses_api_bridge_check
        from litellm.utils import peek_reasoning_summary_aliases
        if kwargs.get("_skip_responses_api_bridge"):
            return "provider"
        info, _ = responses_api_bridge_check(model=model, custom_llm_provider=provider,
            web_search_options=kwargs.get("web_search_options"), tools=kwargs.get("tools"),
            reasoning_effort=kwargs.get("reasoning_effort"),
            reasoning_summary=peek_reasoning_summary_aliases(dict(kwargs)), api_base=kwargs.get("api_base"))
        if info.get("mode") == "responses":
            return "bridge"
    return "provider"


@dataclass(frozen=True)
class Trace:
    request_id: str
    environment: str
    key_id: str = "local-unattributed"
    customer_id: str | None = None
    parent_attempt_id: str | None = None
    api_path: str = "/local"
    role: str = "inference"


trace_context: ContextVar[Trace | None] = ContextVar("subroute_usage_trace", default=None)
role_context: ContextVar[str | None] = ContextVar("subroute_usage_role", default=None)
direct_context: ContextVar["DirectAttempt | None"] = ContextVar("subroute_direct_attempt", default=None)
deployment_context: ContextVar["DirectAttempt | None"] = ContextVar("subroute_deployment_attempt", default=None)
native_advisor_context: ContextVar[bool] = ContextVar("subroute_native_advisor_orchestration", default=False)
native_advisor_parent_role: ContextVar[str | None] = ContextVar("subroute_native_advisor_parent_role", default=None)
native_advisor_attempts: ContextVar[list["DirectAttempt"] | None] = ContextVar("subroute_native_advisor_attempts", default=None)


def local_trace() -> Trace:
    environment = os.getenv("SUBROUTE_ENVIRONMENT", "production")
    if environment not in {"production", "staging"}:
        raise HTTPException(503, "Invalid accounting environment")
    return Trace(str(uuid.uuid4()), environment)


def local_api_path(path: str) -> str:
    suffix = path.removeprefix("/v1/") if path.startswith("/v1/") else path.removeprefix("/")
    return "/v1/" + suffix if suffix in LOCAL_INFERENCE_PATHS else "/local"


def verify_grant(value: str, secret: str, path: str) -> Trace:
    """A grant is base64url(JSON).hex(HMAC-SHA256), with a short admission TTL."""
    try:
        if not secret or len(secret) < 32 or len(value) > 2048:
            raise ValueError()
        encoded, supplied = value.split(".")
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError()
        grant = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(grant, dict):
            raise ValueError()
        current = int(time.time())
        if (type(grant.get("iat")) is not int or type(grant.get("exp")) is not int
                or not current - 120 <= grant["iat"] <= current + 10
                or not current < grant["exp"] <= grant["iat"] + 120
                or grant.get("environment") not in {"production", "staging"}
                or grant.get("api_path") != path or grant.get("role") not in ROLES):
            raise ValueError()
        request_id = str(uuid.UUID(grant["request_id"]))
        parent = str(uuid.UUID(grant["parent_attempt_id"]))
        key_id = grant.get("key_id")
        customer = grant.get("customer_id")
        if not identity(key_id) or (customer is not None and not identity(customer)):
            raise ValueError()
        return Trace(request_id, grant["environment"], key_id, customer, parent, path, grant["role"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(401, "Invalid accounting grant") from None


class ReceiptStore:
    """Use the proxy's generated Prisma connection, with bounded SQL calls."""
    def __init__(self, db: Any = None):
        self._db = db
        self._ready = False
        self._lock = asyncio.Lock()

    @property
    def db(self):
        if self._db is not None:
            return self._db
        from litellm.proxy import proxy_server
        client = proxy_server.prisma_client
        if client is None:
            raise HTTPException(503, "Usage storage unavailable")
        return client.db

    async def execute(self, sql, *args):
        async with asyncio.timeout(3):
            return await self.db.execute_raw(sql, *args)

    async def query(self, sql, *args):
        async with asyncio.timeout(3):
            return await self.db.query_raw(sql, *args)

    async def ensure(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self.execute(SCHEMA)
                await self.execute("CREATE INDEX IF NOT EXISTS subroute_usage_outbox ON subroute_usage_receipts(environment,updated_at,attempt_id) WHERE revision>acked_revision")
                self._ready = True

    async def start(self, trace: Trace, model: str, provider: str, role: str, kind: str = "provider") -> str:
        attempt_id = str(uuid.uuid4())
        payload = {
            "attempt_id": attempt_id, "request_id": trace.request_id,
            "parent_attempt_id": trace.parent_attempt_id, "environment": trace.environment,
            "customer_id": trace.customer_id, "key_id": trace.key_id,
            "api_path": trace.api_path, "role": role,
            "provider_id": label(provider) or "unknown", "upstream_model": label(model),
            "kind": kind, "source": "subroute", "created_at": now_ms(),
            "usage_complete": False,
        }
        try:
            await self.ensure()
            await self.execute("""INSERT INTO subroute_usage_receipts
                (attempt_id,environment,request_id,parent_attempt_id,customer_id,key_id,outcome,payload,updated_at)
                VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,'pending',$7::jsonb,$8)""",
                attempt_id, trace.environment, trace.request_id, trace.parent_attempt_id,
                trace.customer_id, trace.key_id, json.dumps(payload), now_ms())
        except Exception:
            raise HTTPException(503, "Usage storage unavailable") from None
        return attempt_id

    async def update(self, attempt_id: str, facts: dict, outcome: str = "pending") -> bool:
        try:
            await self.execute("""UPDATE subroute_usage_receipts SET
                outcome=CASE WHEN outcome='pending' THEN $2 ELSE outcome END,
                payload=payload || $3::jsonb, revision=revision+1, updated_at=$4
                WHERE attempt_id=$1::uuid AND outcome IN ('pending','interrupted')""",
                attempt_id, outcome, json.dumps(facts), now_ms())
            return True
        except Exception as exc:
            logger.error("usage receipt write failed attempt_id=%s error_type=%s", attempt_id, type(exc).__name__)
            return False

    async def checkpoint(self, attempt_id: str, facts: dict):
        try:
            await self.execute("""UPDATE subroute_usage_receipts SET payload=payload || $2::jsonb,
                revision=revision+1,updated_at=$3 WHERE attempt_id=$1::uuid AND outcome='pending'
                AND NOT payload @> $2::jsonb
                AND (updated_at<$4 OR NOT (payload ? 'input_tokens' OR payload ? 'output_tokens'))""",
                attempt_id, json.dumps(facts), now_ms(), now_ms()-15_000)
            return True
        except Exception:
            logger.error("usage checkpoint write failed attempt_id=%s", attempt_id)
            return False

    async def mirror_advisor_log(self, attempt_id: str | None = None) -> bool:
        """Make completed direct Advisor usage visible in LiteLLM's Logs UI.

        The receipt remains authoritative. A distinct request ID and zero spend
        when provider cost is unreported avoid double charging the parent call.
        Passing None backfills existing receipts idempotently.
        """
        try:
            await self.execute("""INSERT INTO "LiteLLM_SpendLogs"
                (request_id,call_type,api_key,spend,total_tokens,prompt_tokens,
                 completion_tokens,"startTime","endTime",model,model_group,
                 custom_llm_provider,metadata,status)
                SELECT 'subroute-advisor-' || r.attempt_id::text, 'advisor', '',
                  COALESCE((r.payload->>'provider_cost_micros')::double precision / 1000000, 0),
                  COALESCE((r.payload->>'total_tokens')::integer,
                    (r.payload->>'input_tokens')::integer + (r.payload->>'output_tokens')::integer),
                  (r.payload->>'input_tokens')::integer,
                  (r.payload->>'output_tokens')::integer,
                  to_timestamp((r.payload->>'created_at')::bigint / 1000.0) AT TIME ZONE 'UTC',
                  to_timestamp(r.updated_at / 1000.0) AT TIME ZONE 'UTC',
                  COALESCE(r.payload->>'upstream_model','unknown'),
                  COALESCE(r.payload->>'upstream_model','unknown'),
                  COALESCE(r.payload->>'provider_id','unknown'),
                  jsonb_build_object(
                    'subroute_role','advisor',
                    'subroute_attempt_id',r.attempt_id::text,
                    'subroute_request_id',r.request_id::text,
                    'usage_complete',true,
                    'provider_cost_reported',r.payload ? 'provider_cost_micros',
                    'spend_logs_metadata',jsonb_build_object('role','advisor','source','subroute')),
                  'success'
                FROM subroute_usage_receipts r
                WHERE r.payload->>'role'='advisor' AND r.payload->>'kind'='provider'
                  AND r.outcome='complete' AND r.payload->>'usage_complete'='true'
                  AND ($1::uuid IS NULL OR r.attempt_id=$1::uuid)
                ON CONFLICT (request_id) DO NOTHING""", attempt_id)
            return True
        except Exception:
            logger.error("advisor Logs mirror failed attempt_id=%s", attempt_id)
            return False

    async def pending(self, environment: str) -> list:
        await self.ensure()
        # Terminal state changes leave a revision for the consumer, even after restart.
        await self.execute("""UPDATE subroute_usage_receipts SET outcome='interrupted',
            revision=revision+1,updated_at=$1 WHERE attempt_id IN
            (SELECT attempt_id FROM subroute_usage_receipts WHERE environment=$2
             AND outcome='pending' AND updated_at<$3 ORDER BY updated_at LIMIT 100)""",
            now_ms(), environment, now_ms() - 900_000)
        rows = await self.query("""SELECT payload || jsonb_build_object('revision',revision,
            'outcome',outcome,'updated_at',updated_at) AS receipt FROM subroute_usage_receipts
            WHERE environment=$1 AND revision>acked_revision
            ORDER BY updated_at,attempt_id LIMIT 100""", environment)
        return [json.loads(row["receipt"]) if isinstance(row["receipt"], str) else row["receipt"] for row in rows]

    async def ack(self, environment: str, receipts: list):
        for item in receipts:
            await self.execute("""UPDATE subroute_usage_receipts SET acked_revision=greatest(acked_revision,$3)
                WHERE environment=$1 AND attempt_id=$2::uuid AND revision>=$3""",
                environment, item["attempt_id"], item["revision"])


def attempt_id(kwargs: Mapping) -> str | None:
    if isinstance(kwargs.get(TAG), str):
        return kwargs[TAG]
    params = kwargs.get("litellm_params") or kwargs
    for field in ("metadata", "litellm_metadata"):
        data = params.get(field) if isinstance(params, Mapping) else None
        if isinstance(data, Mapping) and isinstance(data.get(TAG), str):
            return data[TAG]
    return None


class UsageAccounting(CustomLogger):
    def __init__(self, store: ReceiptStore | None = None):
        super().__init__(turn_off_message_logging=False)
        # We extract only numeric facts ourselves. Framework redaction replaces
        # the whole response with an empty object if this logger opts into it.
        self.store = store or ReceiptStore()

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        metadata = data.get("litellm_metadata") or data.get("metadata") or {}
        # Only a framework-owned orchestration scope can inherit a parent.
        # A caller's advisor_sub_call metadata does not establish that scope.
        if native_advisor_context.get() and trace_context.get() is not None:
            return None
        for field in ("metadata", "litellm_metadata"):
            bucket = data.get(field)
            if isinstance(bucket, dict):
                bucket.pop(TAG, None)
        data.pop(TAG, None)
        proxy = data.get("proxy_server_request") or {}
        headers = {str(k).lower(): v for k, v in (proxy.get("headers") or {}).items()}
        path = str(proxy.get("url", "")).split("?", 1)[0]
        from urllib.parse import urlsplit
        path = urlsplit(path).path or "/local"
        grant = headers.get("x-subroute-accounting-grant")
        if grant:
            trace = verify_grant(grant, os.getenv("SUBROUTE_ACCOUNTING_SECRET", ""), path)
            # Do not let the signed bearer grant enter another logger's metadata.
            for name in list(proxy.get("headers") or {}):
                if name.lower() in {"x-subroute-accounting-grant", "x-subroute-accounting-token"}:
                    proxy["headers"].pop(name, None)
        else:
            trace = local_trace()
            # Only an auth object created by LiteLLM is authoritative. A
            # presented header alone is never proof on keyless loopback.
            from litellm.proxy._types import UserAPIKeyAuth
            if isinstance(user_api_key_dict, UserAPIKeyAuth) and user_api_key_dict.via_virtual_key and user_api_key_dict.api_key:
                trace = Trace(trace.request_id, trace.environment,
                              "local-sha256:" + hashlib.sha256(user_api_key_dict.api_key.encode()).hexdigest(),
                              api_path=local_api_path(path))
            else:
                trace = Trace(trace.request_id, trace.environment, api_path=local_api_path(path))
        trace_context.set(trace)
        deployment_context.set(None)
        return None

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        # LiteLLM's web-search interception invokes asearch internally using a
        # search provider and query, not an LLM model. Search-tool execution is
        # accounted by LiteLLM's search path and has no provider token receipt
        # for this model-usage ledger to create.
        call = getattr(call_type, "value", call_type)
        if call in {"search", "asearch"}:
            return kwargs
        if call in {"anthropic_messages", "aanthropic_messages"}:
            # LiteLLM invokes this hook for the intercepted public Messages
            # request. It is orchestration, not a provider attempt; the native
            # advisor executor's own hook records the actual provider call.
            # Retire if LiteLLM stops invoking deployment hooks for this outer request.
            from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import AdvisorOrchestrationHandler
            from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
            _, provider, _, _ = get_llm_provider(
                model=str(kwargs.get("model") or ""),
                custom_llm_provider=kwargs.get("custom_llm_provider"),
                api_base=kwargs.get("api_base"),
            )
            if AdvisorOrchestrationHandler().can_handle(kwargs.get("tools"), provider):
                deployment_context.set(None)
                return kwargs
        trace = trace_context.get() or local_trace()
        metadata = kwargs.get("litellm_metadata") or kwargs.get("metadata") or {}
        role = role_context.get() or trace.role
        orchestration_metadata = kwargs.get("metadata") or {}
        advisor_flag = orchestration_metadata.get("advisor_sub_call", metadata.get("advisor_sub_call"))
        if native_advisor_context.get() and type(advisor_flag) is bool:
            role = "advisor" if advisor_flag else native_advisor_parent_role.get() or trace.role
            # The framework's translated inner deployment omits its advisor
            # flag. Carry the chosen role through that adapter until the next
            # framework executor call restores the parent role.
            role_context.set(role)
        model = str(kwargs.get("model") or "unknown")
        provider = str(kwargs.get("custom_llm_provider") or model.split("/", 1)[0])
        kind = deployment_kind(kwargs, call_type)
        ident = await self.store.start(trace, model, provider, role, kind)
        attempt = DirectAttempt(ident, self.store)
        deployment_context.set(attempt)
        attempts = native_advisor_attempts.get()
        if attempts is not None:
            attempts.append(attempt)
        # Metadata is copied per attempt, never a shared fallback mutation.
        field = "litellm_metadata" if "litellm_metadata" in kwargs else "metadata"
        kwargs[field] = {**metadata, TAG: ident}
        logging_obj = kwargs.get("litellm_logging_obj")
        if logging_obj is not None:
            logging_obj.model_call_details[TAG] = ident
            logging_obj.model_call_details["_subroute_observed_usage"] = {}
        return kwargs

    async def async_post_call_success_deployment_hook(self, request_data, response, call_type):
        # Raw stream observers own settlement, including EOF/cancellation and
        # usage before LiteLLM estimates it. In 1.101.0/1.103.0 Responses also
        # invokes this redundant hook via run_async_function, blocking the
        # serving loop while the hook runs in a worker loop. Prisma's async
        # pool cannot be used there. Keep writes on the existing observer path.
        if request_data.get("_subroute_stream_observed") is True:
            return
        ident = attempt_id(request_data)
        if ident:
            facts = usage_facts(response)
            outcome = response_outcome(response)
            facts["usage_complete"] = outcome == "complete" and "input_tokens" in facts and "output_tokens" in facts
            await self.store.update(ident, facts, outcome)

    async def async_post_call_failure_deployment_hook(self, request_data, exception, call_type, fallback_depth=None):
        ident = attempt_id(request_data)
        if ident:
            captured = deployment_context.get()
            facts = dict(captured.facts) if captured and captured.ident == ident else {}
            facts.update(usage_facts({"usage": getattr(exception, "provider_usage", None)}))
            await self.store.update(ident, facts, "failed")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        # Observed streams are settled by their parser/close observer. The
        # aggregate logger is a duplicate writer and may run after settlement.
        if kwargs.get("_subroute_stream_observed") is True and kwargs.get("cache_hit") is not True:
            return
        ident = attempt_id(kwargs)
        if ident:
            if kwargs.get("cache_hit") is True:
                await self.store.update(ident, {"usage_complete": False}, "cached")
            else:
                facts = dict(kwargs.get("_subroute_observed_usage", {})) if kwargs.get("stream") is True or kwargs.get("_subroute_stream_observed") else usage_facts(response_obj)
                outcome = kwargs.get("_subroute_stream_outcome", response_outcome(response_obj))
                facts["usage_complete"] = outcome == "complete" and "input_tokens" in facts and "output_tokens" in facts
                await self.store.update(ident, facts, outcome)


usage_accounting = UsageAccounting()


def _require_hook(owner: type, name: str, *required_parameters: str) -> None:
    """Reject framework drift that makes a required accounting hook unusable."""
    hook = getattr(owner, name, None)
    if not callable(hook):
        raise RuntimeError(f"LiteLLM accounting hook unavailable: {owner.__name__}.{name}")
    try:
        parameters = inspect.signature(hook).parameters
        if not set(required_parameters).issubset(parameters):
            raise TypeError("required parameters missing")
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"LiteLLM accounting hook signature changed: {owner.__name__}.{name}") from exc


def install_stream_accounting():
    """Retain provider facts before LiteLLM substitutes token-count estimates.

    The installed deployment streaming hook runs only on the final chunk, after
    usage stripping. Its success callback receives an assembled response that
    estimates missing usage. This narrow capability-checked observation preserves
    the framework's output and owns no protocol parsing or provider transport.
    Retire when LiteLLM exposes provider-origin usage and cancellation per chunk.
    """
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    if getattr(CustomStreamWrapper, "_subroute_usage_observer", False):
        return
    # LiteLLM version numbers are not a compatibility contract. Fail closed
    # only when a hook this observer actually wraps disappears or can no longer
    # be called with the arguments used below. Behavioral compatibility is
    # exercised by the real Router/stream tests in test_usage_accounting.py.
    _require_hook(CustomStreamWrapper, "chunk_creator", "self", "chunk")
    _require_hook(CustomStreamWrapper, "__anext__", "self")
    _require_hook(CustomStreamWrapper, "aclose", "self")
    _require_hook(CustomStreamWrapper, "__init__", "self", "completion_stream", "model", "logging_obj")
    original_chunk = CustomStreamWrapper.chunk_creator
    original_next = CustomStreamWrapper.__anext__
    original_close = CustomStreamWrapper.aclose
    original_init = CustomStreamWrapper.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._subroute_stream_attempt = attempt_id(self.logging_obj.model_call_details)
        self._subroute_raw_usage = {}
        # Router's FallbackStreamWrapper delegates iteration to the actual
        # provider wrapper, but shares its logging object. Its own empty flags
        # must never settle that provider's attempt during aggregate cleanup.
        self._subroute_stream_owner = type(self).__anext__ is next_chunk
        self._subroute_provider_terminal = False
        if self._subroute_stream_attempt and self._subroute_stream_owner:
            details = self.logging_obj.model_call_details
            details["_subroute_stream_observed"] = True
            details["_subroute_stream_outcome"] = "interrupted"

    def provider_terminal(chunk):
        raw = as_dict(chunk)
        if raw.get("is_finished") is True and isinstance(raw.get("finish_reason"), str) and raw["finish_reason"]:
            return True
        choices = raw.get("choices")
        return isinstance(choices, list) and any(
            isinstance(as_dict(choice).get("finish_reason"), str) and as_dict(choice)["finish_reason"]
            for choice in choices)

    def chunk_creator(self, chunk):
        before = len(self.chunks)
        details = self.logging_obj.model_call_details
        owned = self._subroute_stream_owner and bool(self._subroute_stream_attempt)
        if owned and provider_terminal(chunk):
            # Inspect the provider value before LiteLLM strips its finish
            # reason or synthesizes a terminal chunk for a bare HTTP EOF.
            self._subroute_provider_terminal = True
            details["_subroute_stream_outcome"] = "complete"
        result = None
        try:
            result = original_chunk(self, chunk)
        finally:
            if owned:
                capture(self, chunk, result, before, details)
        return result

    def capture(self, chunk, result, before, details):
        # A usage-only frame may be retained in self.chunks and not returned.
        # The custom-provider parser converts explicit zero to None; keep
        # the original recognized usage fields when they are available.
        raw = as_dict(chunk)
        raw_has_usage = isinstance(raw.get("usage"), (dict,)) or raw.get("usage") is not None
        facts = usage_facts(chunk) if raw_has_usage else usage_facts(result)
        for retained in self.chunks[before:]:
            if not raw_has_usage:
                facts.update(usage_facts(retained))
        self._subroute_raw_usage.update(facts)
        details["_subroute_observed_usage"] = dict(self._subroute_raw_usage)

    async def checkpoint(self):
        details = self.logging_obj.model_call_details
        ident = self._subroute_stream_attempt
        facts = self._subroute_raw_usage
        saved = getattr(self, "_subroute_saved_usage", {})
        last = getattr(self, "_subroute_checkpoint_at", float("-inf"))
        count = getattr(self, "_subroute_checkpoint_count", 0)
        if ident and facts and facts != saved and count < 41 and time.monotonic()-last >= 15:
            self._subroute_checkpoint_at = time.monotonic()
            self._subroute_checkpoint_count = count+1
            if await usage_accounting.store.checkpoint(ident, facts):
                self._subroute_saved_usage = dict(facts)

    async def finish(self, outcome):
        details = self.logging_obj.model_call_details
        ident = self._subroute_stream_attempt
        if ident and self._subroute_stream_owner:
            outcome = "complete" if self._subroute_provider_terminal else outcome
            details["_subroute_stream_outcome"] = outcome
            facts = dict(self._subroute_raw_usage)
            facts["usage_complete"] = outcome == "complete" and "input_tokens" in facts and "output_tokens" in facts
            await usage_accounting.store.update(ident, facts, outcome)

    async def next_chunk(self):
        try:
            result = await original_next(self)
        except asyncio.CancelledError:
            await finish(self, "cancelled")
            raise
        except StopAsyncIteration:
            await finish(self, "interrupted")
            raise
        except Exception:
            await finish(self, "failed")
            raise
        await checkpoint(self)
        return result

    async def close(self):
        try:
            return await original_close(self)
        finally:
            # A close is not itself evidence of a client disconnect. Only the
            # provider parser owns settlement; real cancellation is caught in
            # iteration, and clean EOF without a provider terminal is partial.
            await finish(self, "interrupted")

    CustomStreamWrapper.chunk_creator = chunk_creator
    CustomStreamWrapper.__init__ = init
    CustomStreamWrapper.__anext__ = next_chunk
    CustomStreamWrapper.aclose = close
    CustomStreamWrapper._subroute_usage_observer = True
    install_native_stream_accounting()
    install_native_advisor_accounting()


def install_native_advisor_accounting():
    from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import AdvisorOrchestrationHandler
    _require_hook(AdvisorOrchestrationHandler, "handle", "self")
    original = AdvisorOrchestrationHandler.handle

    async def handle(self, *args, **kwargs):
        token = native_advisor_context.set(True)
        attempts = []
        attempts_token = native_advisor_attempts.set(attempts)
        trace = trace_context.get()
        parent = native_advisor_parent_role.set(role_context.get() or (trace.role if trace else "inference"))
        role_token = role_context.set(role_context.get())
        try:
            return await original(self, *args, **kwargs)
        except asyncio.CancelledError:
            # LiteLLM 1.106.2 omits failure hooks when buffered inner HTTP is
            # cancelled. Retire this cleanup when native hooks settle those
            # attempts. The store leaves completed records unchanged.
            for attempt in attempts:
                await attempt.store.update(attempt.ident, {"usage_complete": False}, "cancelled")
            raise
        finally:
            native_advisor_attempts.reset(attempts_token)
            role_context.reset(role_token)
            native_advisor_parent_role.reset(parent)
            native_advisor_context.reset(token)

    AdvisorOrchestrationHandler.handle = handle


def install_native_stream_accounting():
    """Observe native frames before framework price injection or assembly."""
    from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator, ResponsesAPIStreamingIterator
    from litellm.proxy.pass_through_endpoints.streaming_handler import PassThroughStreamingHandler
    from openai._streaming import SSEDecoder
    import codecs

    original_process = BaseResponsesAPIStreamingIterator._process_chunk
    original_next = ResponsesAPIStreamingIterator.__anext__
    original_close = getattr(ResponsesAPIStreamingIterator, "aclose", None)
    original_messages = PassThroughStreamingHandler.chunk_processor
    _require_hook(BaseResponsesAPIStreamingIterator, "_process_chunk", "self", "chunk")
    _require_hook(ResponsesAPIStreamingIterator, "__anext__", "self")
    _require_hook(PassThroughStreamingHandler, "chunk_processor",
                  "response", "request_body", "litellm_logging_obj")

    def observation(details):
        ident = attempt_id(details)
        if not ident:
            return None
        details["_subroute_stream_observed"] = True
        details["_subroute_stream_outcome"] = "interrupted"
        observer = DirectAttempt(ident, usage_accounting.store)
        observer.facts.update(details.get("_subroute_observed_usage", {}))
        return observer

    def observe_event(observer, details, value):
        if observer is None or not isinstance(value, dict):
            return
        observer.facts.update(usage_facts(value))
        details["_subroute_observed_usage"] = dict(observer.facts)
        kind = value.get("type")
        if kind in {"message_stop", "response.completed"}:
            details["_subroute_stream_outcome"] = "complete"
        elif kind in {"error", "response.failed"}:
            details["_subroute_stream_outcome"] = "failed"
        elif kind == "response.incomplete":
            details["_subroute_stream_outcome"] = "interrupted"

    async def finish(observer, details, outcome):
        if observer:
            details["_subroute_stream_outcome"] = outcome
            facts = dict(observer.facts)
            facts["usage_complete"] = outcome == "complete" and "input_tokens" in facts and "output_tokens" in facts
            await observer.store.update(observer.ident, facts, outcome)

    def process(self, chunk):
        details = self.logging_obj.model_call_details
        if not hasattr(self, "_subroute_native_observer"):
            self._subroute_native_observer = observation(details)
        provider_config = self.responses_api_provider_config

        class ObservedConfig:
            def __getattr__(_self, name):
                return getattr(provider_config, name)

            def transform_streaming_response(_self, *args, **kwargs):
                # Reuse the framework's parsed object, including a long final
                # output snapshot. Do not copy or persist its content or allow
                # later framework price injection to become a provider fact.
                observe_event(self._subroute_native_observer, details, kwargs.get("parsed_chunk"))
                return provider_config.transform_streaming_response(*args, **kwargs)

        if provider_config is not None:
            self.responses_api_provider_config = ObservedConfig()
        try:
            return original_process(self, chunk)
        finally:
            self.responses_api_provider_config = provider_config

    async def responses_next(self):
        details = self.logging_obj.model_call_details
        try:
            result = await original_next(self)
        except StopAsyncIteration:
            await finish(getattr(self, "_subroute_native_observer", None), details, details.get("_subroute_stream_outcome", "interrupted"))
            raise
        except asyncio.CancelledError:
            await finish(getattr(self, "_subroute_native_observer", None), details, "cancelled")
            raise
        except Exception:
            await finish(getattr(self, "_subroute_native_observer", None), details, "failed")
            raise
        observer = getattr(self, "_subroute_native_observer", None)
        if observer:
            await observer.checkpoint()
        return result

    async def responses_close(self):
        if not hasattr(self, "logging_obj"):
            # LiteLLM's Chat-to-Responses subclass skips the native base
            # initializer. Its provider accounting lives in the Chat source.
            source = getattr(self, "litellm_custom_stream_wrapper", None)
            close = getattr(source, "aclose", None)
            if close:
                await close()
            return
        details = self.logging_obj.model_call_details
        observer = getattr(self, "_subroute_native_observer", None)
        if observer is None:
            observer = observation(details)
        try:
            if original_close:
                await original_close(self)
            else:
                await self.response.aclose()
        finally:
            await finish(observer, details, details.get("_subroute_stream_outcome") if details.get("_subroute_stream_outcome") in {"complete", "failed"} else "cancelled")

    async def messages(*args, **kwargs):
        # Keep the existing framework transport and output bytes. Only a
        # bounded observation of its raw provider byte iterator is substituted.
        names = ("response", "request_body", "litellm_logging_obj", "endpoint_type", "start_time", "passthrough_success_handler_obj", "url_route", "route_streaming_logging")
        params = {**dict(zip(names, args)), **kwargs}
        response = params["response"]
        details = params["litellm_logging_obj"].model_call_details
        observer = observation(details)
        decoder = SSEDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        event_bytes = 0
        discard_line = False

        class ObservedResponse:
            def __getattr__(self, name):
                return getattr(response, name)

            async def aiter_bytes(self):
                nonlocal pending, event_bytes, discard_line, decoder
                async for chunk in response.aiter_bytes():
                    # Iterate bounded slices so one oversized HTTP chunk cannot
                    # allocate an unbounded secondary telemetry buffer.
                    for offset in range(0, len(chunk), 8192):
                        pending += utf8.decode(chunk[offset:offset+8192])
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            if discard_line:
                                discard_line = False
                                continue
                            event_bytes += len(line)
                            if event_bytes > 65536:
                                decoder, event_bytes = SSEDecoder(), 0
                                continue
                            event = decoder.decode(line.rstrip("\r"))
                            if not line.rstrip("\r"):
                                event_bytes = 0
                            if event:
                                try:
                                    observe_event(observer, details, json.loads(event.data))
                                except (ValueError, TypeError):
                                    pass
                        if len(pending) > 65536:
                            pending, discard_line, decoder, event_bytes = "", True, SSEDecoder(), 0
                    if observer:
                        await observer.checkpoint()
                    yield chunk

        params["response"] = ObservedResponse()
        outcome = "interrupted"
        source = original_messages(**params)
        try:
            async for chunk in source:
                yield chunk
            outcome = details.get("_subroute_stream_outcome", "interrupted")
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            await source.aclose()
            await finish(observer, details, outcome)

    BaseResponsesAPIStreamingIterator._process_chunk = process
    ResponsesAPIStreamingIterator.__anext__ = responses_next
    ResponsesAPIStreamingIterator.aclose = responses_close
    PassThroughStreamingHandler.chunk_processor = staticmethod(messages)


@contextmanager
def accounting_role(role: str):
    token = role_context.set(role)
    try:
        yield
    finally:
        role_context.reset(token)


class DirectAttempt:
    def __init__(self, ident: str, store: ReceiptStore):
        self.ident, self.store = ident, store
        self.facts: dict = {}
        self.saved: dict = {}
        self.last_write = 0.0
        self.checkpoints = 0

    async def observe(self, value):
        self.facts.update(usage_facts(value))
        await self.checkpoint()

    async def checkpoint(self):
        if self.facts != self.saved and self.checkpoints < 41 and time.monotonic() - self.last_write >= 15:
            self.last_write = time.monotonic()
            self.checkpoints += 1
            if await self.store.update(self.ident, self.facts):
                self.saved = self.facts.copy()


async def observe_direct(value):
    attempt = direct_context.get() or deployment_context.get()
    if attempt:
        await attempt.observe(value)


@asynccontextmanager
async def direct_attempt(model: str, provider: str, role: str):
    # Tests calling an individual helper outside the proxy do not implicitly
    # connect to a deployment database. Every configured proxy request sets a trace.
    trace = trace_context.get()
    if trace is None:
        yield None
        return
    store = usage_accounting.store
    ident = await store.start(trace, model, provider, role)
    attempt = DirectAttempt(ident, store)
    token = direct_context.set(attempt)
    outcome = "complete"
    try:
        yield attempt
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException as exc:
        outcome = "failed"
        attempt.facts.update(usage_facts({"usage": getattr(exc, "provider_usage", None)}))
        raise
    finally:
        direct_context.reset(token)
        attempt.facts["usage_complete"] = outcome == "complete" and "input_tokens" in attempt.facts and "output_tokens" in attempt.facts
        saved = await store.update(ident, attempt.facts, outcome)
        if saved and role == "advisor" and attempt.facts["usage_complete"]:
            await store.mirror_advisor_log(ident)


def install_receipt_routes(app):
    """Explicit installation alongside the existing routing control surface."""
    async def receipts(request: Request):
        secret = os.getenv("SUBROUTE_ACCOUNTING_SECRET", "")
        supplied = request.headers.get("x-subroute-accounting-token", "")
        if len(secret) < 32 or not hmac.compare_digest(hashlib.sha256(secret.encode()).digest(), hashlib.sha256(supplied.encode()).digest()):
            raise HTTPException(401, "Accounting authorization required")
        environment = request.query_params.get("environment")
        if environment not in {"production", "staging"}:
            raise HTTPException(400, "Invalid accounting environment")
        try:
            if request.method == "GET":
                return {"receipts": await usage_accounting.store.pending(environment)}
            if int(request.headers.get("content-length", "0")) > 16384:
                raise HTTPException(413, "Acknowledgement too large")
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    raise HTTPException(413, "Acknowledgement too large")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError()
            items = payload.get("receipts")
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError()
            for item in items:
                if not isinstance(item, dict) or type(item.get("revision")) is not int or not 1 <= item["revision"] <= 2_147_483_647:
                    raise ValueError()
                uuid.UUID(item["attempt_id"])
            await usage_accounting.store.ack(environment, items)
            return {"ok": True}
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "Invalid receipt acknowledgement") from None
    for path, method in (("/internal/accounting/receipts", "GET"), ("/internal/accounting/ack", "POST")):
        if not any(getattr(route, "path", None) == path for route in app.routes):
            app.add_api_route(path, receipts, methods=[method], include_in_schema=False)
