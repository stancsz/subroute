"""Sift integration for local Qwen, automatic recovery and target requests."""

import threading
import asyncio
from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sift.litellm_proxy import SiftLiteLLMHook


def _require_sift_budget(result: dict | None, target: int) -> dict:
    """Fail closed unless Sift reports the exact forwarded copy within budget."""
    if not isinstance(result, dict):
        raise HTTPException(503, detail="Sift did not return a forwarding audit")
    metadata = result.get("litellm_metadata") or result.get("metadata") or {}
    audit = metadata.get("sift") if isinstance(metadata, dict) else None
    forwarded = audit.get("forwarded_tokens") if isinstance(audit, dict) else None
    if type(forwarded) is not int or forwarded > target:
        raise HTTPException(413, detail={
            "error": "sift_forwarding_budget_exceeded",
            "limit": target,
            "forwarded_tokens": forwarded if type(forwarded) is int else None,
        })
    return result


class AutoContextRecovery(SiftLiteLLMHook):
    """Compact auto's history before admission/advisor work, with output headroom.

    The core and token counter remain Sift/LiteLLM-owned. This adapter only owns
    the auto route's budget and safe failure contract; it never retries inference.
    """

    def __init__(self) -> None:
        # The inherited response hook uses this map to expose Sift's audit.
        super().__init__({"auto": 204_800}, raw_limit=1_000_000)

    # LiteLLM detects header hooks on the leaf class's __dict__.
    async_post_call_response_headers_hook = SiftLiteLLMHook.async_post_call_response_headers_hook

    @staticmethod
    def input_target(router: Any, data: dict) -> int:
        deployment = router.get_deployment_by_model_group_name(model_group_name="auto")
        info = deployment.model_info if deployment is not None else {}
        limits = [info.get(name) for name in (
            "max_input_tokens", "context_window", "auto_compact_token_limit",
        )]
        if any(type(value) is not int or value <= 0 for value in limits):
            raise HTTPException(503, detail="Automatic context recovery limits are unavailable")
        output_limits = [data[name] for name in ("max_tokens", "max_completion_tokens")
                         if name in data and data[name] is not None]
        if any(type(value) is not int or value <= 0 for value in output_limits):
            raise HTTPException(400, detail="Output token limits must be positive integers")
        # Missing Chat limits retain provider defaults. Reserve 64K rather than
        # silently setting/clamping a caller's output budget. Leave 8K for the
        # existing advisor's later guidance and protocol overhead.
        output = max(output_limits, default=65_536)
        target = min(limits[0], limits[2], limits[1] - output) - 8_192
        if target <= 256:
            raise HTTPException(400, detail="Requested output leaves no safe context budget")
        # Auto retains its declared model capacity, but target calls have a
        # smaller per-request forwarding budget. Do not alter output limits.
        return min(target, 32_768)

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                  data: dict, call_type: str) -> dict | None:
        if data.get("model") != "auto" or call_type not in {
            "completion", "acompletion", "anthropic_messages", "aanthropic_messages",
        }:
            return None
        from litellm.proxy import proxy_server

        router = proxy_server.llm_router
        if router is None:
            raise HTTPException(503, detail="Automatic context recovery counter is unavailable")
        target = self.input_target(router, data)

        try:
            result = await self._compact(router, data, call_type=call_type, target=target)
            return _require_sift_budget(result, target)
        except HTTPException as error:
            if error.status_code not in {413, 422}:
                raise
            unsupported = error.status_code == 422
            raise HTTPException(400, detail={"error": {
                "type": "invalid_request_error",
                "code": "unsupported_context_content" if unsupported else "outbound_context_limit_exceeded",
                "reason": "context_pruning_failed",
                "limit": target, "outbound_tokens": None,
                "message": "History could not be compacted safely; reduce required content or start a new session.",
            }}) from error


class QwenContextRecovery(SiftLiteLLMHook):
    """Own bounded compaction admission for local Qwen requests."""

    def __init__(self) -> None:
        # Native Sift requires an explicit alias map. Configuration tests check
        # it against the authoritative deployment budget in litellm.yaml.
        super().__init__({"qwen3.8-27b": 32_768}, raw_limit=1_000_000)
        # Vendored Sift 0.10.0 hardcodes two permits and exposes no constructor setting.
        # A real four-request probe received two premature Sift-busy responses.
        # Keep native worker/cancellation cleanup; retire this private override
        # when the pinned Sift constructor supports compaction concurrency.
        self._slots = threading.BoundedSemaphore(4)

    # LiteLLM detects this hook on the leaf class's __dict__.
    async_post_call_response_headers_hook = SiftLiteLLMHook.async_post_call_response_headers_hook

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                  data: dict, call_type: str) -> dict | None:
        model = data.get("model")
        if not isinstance(model, str) or model not in self.model_targets:
            return None
        # Pinned llama.cpp reads only string tool choices and silently treats
        # named objects as "auto". Reject that unsupported contract explicitly.
        # Retire when the pinned native server enforces named tool choices.
        choice = data.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") in {"function", "tool"}:
            raise HTTPException(400, detail="Named tool choice is unsupported by the local Qwen server")
        from litellm.proxy import proxy_server

        router = proxy_server.llm_router
        if router is None:
            raise HTTPException(503, detail="Sift token counter is unavailable")
        _, maximum = router.get_configured_token_limits(model)
        if type(maximum) is not int or maximum <= 0:
            raise HTTPException(503, detail="Local output token limit is unavailable")
        # LiteLLM 1.103.0's admission check ignores max_output_tokens, and the
        # pinned llama.cpp permits per-request overrides of --n-predict.
        # Reject overflow rather than clamping the caller's requested output.
        for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            value = data.get(name)
            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise HTTPException(400, detail=f"{name} must be an integer from 1 to {maximum}")
        result = await super().async_pre_call_hook(user_api_key_dict, cache, data, call_type)
        return _require_sift_budget(result, self.model_targets[model])


class TargetContextRecovery(SiftLiteLLMHook):
    """Keep Sift-supported target requests within a per-request 32K budget."""

    def __init__(self) -> None:
        super().__init__({"target": 32_768}, raw_limit=1_000_000)

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                  data: dict, call_type: str) -> dict | None:
        from litellm.proxy import proxy_server

        model = data.get("model")
        if not isinstance(model, str):
            return None
        if call_type not in {
            "completion", "acompletion", "anthropic_messages", "aanthropic_messages",
            "responses", "aresponses",
        }:
            return None

        response_input = call_type in {"responses", "aresponses"}
        source = data.get("input") if response_input else data.get("messages")
        if not isinstance(source, (str, list)):
            return None

        def has_media(value: Any) -> bool:
            if isinstance(value, dict):
                if value.get("type") in {"image", "image_url", "input_image", "audio",
                                            "input_audio", "output_audio", "video", "file",
                                            "input_video", "computer_screenshot"}:
                    return True
                return any(has_media(item) for item in value.values())
            if isinstance(value, list):
                return any(has_media(item) for item in value)
            if isinstance(value, str):
                return any(marker in value for marker in ("data:image/", "data:audio/", "data:video/"))
            return False
        if has_media([source, data.get("system"), data.get("instructions")]):
            return None
        router = proxy_server.llm_router
        if router is None:
            raise HTTPException(503, detail="Sift token counter is unavailable")

        prepared = deepcopy(data)
        if response_input:
            if prepared.get("instructions") is not None and not isinstance(prepared["instructions"], str):
                return None
            counted = await asyncio.to_thread(
                router._count_pre_call_check_tokens, messages=None,
                input=prepared.get("input"), request_kwargs={**prepared, "model": model},
            )
        else:
            messages = prepared.get("messages")
            if not isinstance(messages, list):
                return None
            counted = await asyncio.to_thread(
                router._count_pre_call_check_tokens, messages=messages, input=None,
                request_kwargs={**prepared, "model": model},
            )

        if counted > 16_384:
            reminder = (
                "For this long visible history, keep a concise Ask / Acceptance / Remaining ledger. "
                "Check each next action against the latest explicit user request and constraints. "
                "If acceptance criteria are missing, ask instead of inventing them. Stop when the "
                "stated acceptance evidence is satisfied."
            )
            if response_input:
                instructions = prepared.get("instructions") or ""
                if "Ask / Acceptance / Remaining" not in instructions:
                    prepared["instructions"] = f"{instructions}\n\n{reminder}".strip()
            elif call_type in {"anthropic_messages", "aanthropic_messages"}:
                system = prepared.get("system")
                if system is None:
                    prepared["system"] = reminder
                elif isinstance(system, str):
                    if "Ask / Acceptance / Remaining" not in system:
                        prepared["system"] = f"{system}\n\n{reminder}"
                elif isinstance(system, list):
                    if not any(
                        isinstance(part, dict) and isinstance(part.get("text"), str)
                        and "Ask / Acceptance / Remaining" in part["text"]
                        for part in system
                    ):
                        system.append({"type": "text", "text": reminder})
                else:
                    return None
            else:
                messages = prepared["messages"]

                def has_reminder(value: Any) -> bool:
                    if isinstance(value, str):
                        return "Ask / Acceptance / Remaining" in value
                    if isinstance(value, dict):
                        return any(has_reminder(item) for item in value.values())
                    if isinstance(value, list):
                        return any(has_reminder(item) for item in value)
                    return False

                already_present = any(
                    isinstance(message, dict) and has_reminder(message.get("content"))
                    for message in messages
                )
                if not already_present:
                    messages.append({"role": "developer", "content": reminder})

        result = await self._compact(router, prepared, call_type=call_type, target=32_768)
        return _require_sift_budget(result, 32_768)

    async def async_post_call_response_headers_hook(
        self, data: dict, user_api_key_dict: Any, response: object,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        metadata = data.get("litellm_metadata") or data.get("metadata") or {}
        audit = metadata.get("sift") if isinstance(metadata, dict) else None
        if not isinstance(audit, dict) or type(audit.get("compacted")) is not bool:
            return None
        return {
            "x-sift-compacted": str(audit["compacted"]).lower(),
            "x-sift-estimated-input-tokens": str(audit["input_tokens"]),
            "x-sift-estimated-forwarded-tokens": str(audit["forwarded_tokens"]),
        }


sift_qwen_hook = QwenContextRecovery()
sift_auto_hook = AutoContextRecovery()
sift_target_hook = TargetContextRecovery()
