"""Optional LiteLLM Proxy callback for Sift's bounded chat compaction.

Configure an instance after any callback that resolves the public model alias.
The core package remains independent of LiteLLM until this module is imported.
"""

from __future__ import annotations

import asyncio
import contextvars
from copy import deepcopy
import json
import logging
import threading
from typing import Any, Mapping

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket

from .core import CompactionError, compact_messages


_PROTOCOLS = {
    "completion": "openai",
    "acompletion": "openai",
    "anthropic_messages": "anthropic",
    "aanthropic_messages": "anthropic",
}


def _countable_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Work around LiteLLM 1.103.0's missing-array-items counter error.

    The repaired schema is used for counting only; provider input stays exact.
    Remove this when the deployed LiteLLM counter accepts such schemas.
    """
    if not isinstance(tools, list):
        return None
    cloned = deepcopy(tools)
    pending: list[Any] = [cloned]
    changed = False
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited > 10_000:
            raise HTTPException(422, detail="tool schema is too large to count")
        if isinstance(node, dict):
            if node.get("type") == "array" and "items" not in node:
                node["items"] = {"type": "string"}
                changed = True
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return cloned if changed else None


def compact_proxy_request(
    router: Any, data: dict[str, Any], *, call_type: str,
    target: int, raw_limit: int = 4_000_000,
) -> dict[str, Any]:
    """Return request messages plus a server-owned Sift audit.

    Uses the proxy router's deployed pre-call counter so tools and route
    metadata count against the same budget as LiteLLM's own capacity check.
    """
    response_input = call_type in {"responses", "aresponses"}
    protocol = "openai" if response_input else _PROTOCOLS.get(call_type)
    if protocol is None:
        raise HTTPException(422, detail="Sift supports Chat Completions, Anthropic Messages and stateless text Responses only")
    restore = None
    if response_input:
        from .litellm_responses import response_selection_view

        messages, restore = response_selection_view(data)
    else:
        messages = data.get("messages")
    if not isinstance(messages, list) or not all(isinstance(msg, dict) for msg in messages):
        raise HTTPException(400, detail="Sift requires chat messages")
    counting_data = data
    if response_input and data.get("tools"):
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )

        # The deployed route counter ignores Responses namespace wrappers and
        # custom-tool descriptions. Count LiteLLM's actual Chat tool shape in
        # a private copy; preserve the caller's definitions in the request.
        tools = data["tools"]
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") not in {"function", "custom", "namespace"}
            for tool in tools
        ):
            raise HTTPException(422, detail="Sift Responses supports function, custom and namespace tools only")
        for tool in tools:
            if tool["type"] == "namespace" and (
                not isinstance(tool.get("tools"), list) or any(
                    not isinstance(child, dict) or child.get("type") != "function"
                    for child in tool["tools"]
                )
            ):
                raise HTTPException(422, detail="Sift requires function definitions inside Responses namespaces")
        try:
            chat_tools, _ = LiteLLMCompletionResponsesConfig.transform_responses_api_tools_to_chat_completion_tools(tools)
        except (TypeError, ValueError, KeyError) as error:
            raise HTTPException(422, detail="Sift cannot count these Responses tools") from error
        counting_data = {**data, "tools": chat_tools}
    repaired_tool_count = False
    tool_floor_adjustment: int | None = None
    raw_input_tokens: int | None = None

    def count(candidate: list[dict[str, Any]]) -> int:
        nonlocal counting_data, repaired_tool_count, tool_floor_adjustment, raw_input_tokens
        counted_input = restore(candidate) if restore else data.get("input")
        request_kwargs = {**counting_data, "input": counted_input} if restore else {**counting_data, "messages": candidate}
        try:
            counted = router._count_pre_call_check_tokens(
                messages=None if restore else candidate, input=counted_input,
                request_kwargs=request_kwargs,
            )
        except KeyError as error:
            if error.args != ("items",) or repaired_tool_count:
                raise
            tools = _countable_tools(counting_data.get("tools"))
            if tools is None:
                raise
            counting_data = {**data, "tools": tools}
            repaired_tool_count = True
            request_kwargs["tools"] = tools
            counted = router._count_pre_call_check_tokens(
                messages=None if restore else candidate, input=counted_input,
                request_kwargs=request_kwargs,
            )
        if tool_floor_adjustment is None:
            tool_floor_adjustment = 0
            if counting_data.get("tools"):
                from litellm import token_counter

                # LiteLLM 1.103.0's formatter skips JSON Schema branches such
                # as $defs/anyOf. A conservative serialized-schema floor keeps
                # their text from consuming uncounted provider context. Charge
                # the larger footprint, rather than double-counting tools.
                schema_tokens = token_counter(text=json.dumps(counting_data["tools"], ensure_ascii=False))
                with_tools = router._count_pre_call_check_tokens(
                    messages=[], input=None, request_kwargs=counting_data,
                )
                without_tools = router._count_pre_call_check_tokens(
                    messages=[], input=None, request_kwargs={**counting_data, "tools": None},
                )
                tool_floor_adjustment = max(0, schema_tokens - max(0, with_tools - without_tools))
        total = counted + tool_floor_adjustment
        if raw_input_tokens is None:
            raw_input_tokens = total
        return total

    try:
        result = compact_messages(
            messages, count, target=target, raw_limit=raw_limit, protocol=protocol,
        )
    except CompactionError as error:
        status = {
            "raw_limit": 413, "active_too_large": 413,
            "unsupported_content": 422, "unsupported_protocol": 422,
            "late_instruction": 422,
        }.get(error.code, 400)
        logging.getLogger("LiteLLM Proxy").warning(
            "sift_compaction_rejected protocol=%s reason=%s input_tokens=%d target=%d raw_limit=%d",
            protocol, error.code, raw_input_tokens if raw_input_tokens is not None else -1,
            target, raw_limit,
        )
        raise HTTPException(status, detail=f"Sift: {error}") from error
    if restore:
        output = {**data, "input": restore(result.messages) if result.audit["compacted"] else data["input"]}
    else:
        output = {**data, "messages": result.messages if result.audit["compacted"] else messages}
    key, metadata = get_or_create_metadata_bucket(output)
    output[key] = {**metadata, "sift": {
        **result.audit, "counting_tool_schema_items_filled": repaired_tool_count,
        "counting_responses_tools_translated": response_input and bool(data.get("tools")),
        "counting_tool_floor_adjustment": tool_floor_adjustment or 0,
    }}
    output_limit = data.get("max_output_tokens", data.get("max_completion_tokens", data.get("max_tokens")))
    logging.getLogger("LiteLLM Proxy").info(
        "sift_request protocol=%s compacted=%s active_tool_results_compacted=%d input_tokens=%d forwarded_tokens=%d target=%d output_limit=%d",
        call_type, str(result.audit["compacted"]).lower(),
        result.audit.get("active_tool_results_compacted", 0), result.audit["input_tokens"],
        result.audit["forwarded_tokens"],
        target, output_limit if type(output_limit) is int else -1,
    )
    return output


class SiftLiteLLMHook(CustomLogger):
    """LiteLLM Proxy callback for explicitly configured chat model names."""

    def __init__(self, model_targets: Mapping[str, int], *, raw_limit: int = 4_000_000) -> None:
        super().__init__()
        if not model_targets or any(
            not isinstance(name, str) or not name or type(target) is not int or target <= 0
            for name, target in model_targets.items()
        ) or type(raw_limit) is not int or raw_limit < max(model_targets.values()):
            raise ValueError("invalid Sift model targets or raw limit")
        self.model_targets = dict(model_targets)
        self.raw_limit = raw_limit
        self._slots = threading.BoundedSemaphore(2)

    @staticmethod
    def request_target(data: dict[str, Any], maximum: int) -> int:
        metadata = data.get("metadata")
        if not isinstance(metadata, dict) or "sift_target_tokens" not in metadata:
            return maximum
        requested = metadata.pop("sift_target_tokens")
        if type(requested) is not int or not 256 <= requested <= maximum:
            raise HTTPException(
                400,
                detail=f"metadata.sift_target_tokens must be an integer from 256 to {maximum}",
            )
        return requested

    async def _compact(self, router: Any, data: dict, *, call_type: str,
                       target: int) -> dict | None:
        """Bound admission before executor submission, including cancelled work.

        A Python thread already running cannot be stopped safely. It keeps its
        slot until it finishes. A cancelled caller's queued worker skips work.
        """
        if not self._slots.acquire(blocking=False):
            raise HTTPException(429, detail="Sift is busy", headers={"Retry-After": "1"})
        abandoned = threading.Event()

        def run() -> dict | None:
            try:
                if abandoned.is_set():
                    return None
                return compact_proxy_request(
                    router, data, call_type=call_type,
                    target=target, raw_limit=self.raw_limit,
                )
            finally:
                self._slots.release()

        try:
            context = contextvars.copy_context()
            worker = asyncio.get_running_loop().run_in_executor(None, context.run, run)
        except BaseException:
            self._slots.release()
            raise
        # Retrieving a detached worker's error avoids an unhandled-future warning;
        # an attached caller still receives the original result or exception.
        worker.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        try:
            # wait() leaves the worker untouched when its caller is cancelled.
            # Unlike shield(), it does not log detached errors again on 3.14.
            await asyncio.wait((worker,))
            return worker.result()
        except asyncio.CancelledError:
            abandoned.set()
            raise

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                  data: dict, call_type: str) -> dict | None:
        model = data.get("model")
        target = self.model_targets.get(model) if isinstance(model, str) else None
        if target is None:
            return None
        target = self.request_target(data, target)
        from litellm.proxy import proxy_server

        router = proxy_server.llm_router
        if router is None:
            raise HTTPException(503, detail="Sift token counter is unavailable")

        return await self._compact(router, data, call_type=call_type, target=target)

    async def async_post_call_response_headers_hook(
        self, data: dict, user_api_key_dict: Any, response: object,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        model = data.get("model")
        if not isinstance(model, str) or model not in self.model_targets:
            return None
        metadata = data.get("litellm_metadata") or data.get("metadata") or {}
        audit = metadata.get("sift") if isinstance(metadata, dict) else None
        if not isinstance(audit, dict) or type(audit.get("compacted")) is not bool:
            return None
        return {
            "x-sift-compacted": str(audit["compacted"]).lower(),
            "x-sift-estimated-input-tokens": str(audit["input_tokens"]),
            "x-sift-estimated-forwarded-tokens": str(audit["forwarded_tokens"]),
        }
