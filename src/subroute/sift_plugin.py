"""Sift integration for local Qwen, automatic recovery and target requests."""

import threading
import asyncio
from copy import deepcopy
import logging
from typing import Any

from fastapi import HTTPException
from sift.litellm_proxy import SiftLiteLLMHook

# LiteLLM extrapolates very large payloads. Keep enough bounded intake headroom
# for a fully tokenized 4M request that its sampler estimates slightly higher.
RAW_INTAKE_LIMIT = 4_194_304


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


def _soft_fail_sift_request(data: dict, *, reason: str, target: int,
                            input_tokens: int | None = None) -> dict:
    """Keep original target traffic flowing when optional Sift work misses."""
    from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket

    data = dict(data)
    key, metadata = get_or_create_metadata_bucket(data)
    metadata = dict(metadata)
    metadata["sift"] = {
        "compacted": False,
        "soft_failed": True,
        "reason": reason,
        "input_tokens": input_tokens if type(input_tokens) is int else -1,
        "forwarded_tokens": input_tokens if type(input_tokens) is int else -1,
        "target": target,
        "max_forwarded_tokens": target * 2,
    }
    data[key] = metadata
    logging.getLogger("LiteLLM Proxy").warning(
        "sift_target_soft_fail reason=%s input_tokens=%d target=%d max_forwarded_tokens=%d",
        reason, input_tokens if type(input_tokens) is int else -1, target, target * 2,
    )
    return data


class AutoContextRecovery(SiftLiteLLMHook):
    """Compact auto's history before admission/advisor work, with output headroom.

    The core and token counter remain Sift/LiteLLM-owned. This adapter only owns
    the auto route's budget and safe failure contract; it never retries inference.
    """

    def __init__(self) -> None:
        # The inherited response hook uses this map to expose Sift's audit.
        super().__init__({"auto": 32_768}, raw_limit=RAW_INTAKE_LIMIT)

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
            result = await self._compact(router, data, call_type=call_type, target=target,
                                         max_target=target * 2)
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {}
            sift_error = detail.get("error") if isinstance(detail.get("error"), dict) else {}
            reason = sift_error.get("reason") or sift_error.get("code")
            if reason == "raw_limit":
                raise HTTPException(400, detail={"error": {
                    "type": "invalid_request_error",
                    "code": "outbound_context_limit_exceeded",
                    "reason": "raw_intake_limit",
                    "limit": self.raw_limit,
                    "outbound_tokens": sift_error.get("input_tokens"),
                    "message": "Request exceeds the configured gateway intake bound.",
                }}) from error
            if error.status_code not in {400, 413, 422, 429, 503}:
                raise
            return _soft_fail_sift_request(
                data, reason=str(reason or f"sift_http_{error.status_code}"), target=target,
                input_tokens=sift_error.get("input_tokens"),
            )

        metadata = (result.get("litellm_metadata") or result.get("metadata") or {}) if isinstance(result, dict) else {}
        audit = metadata.get("sift") if isinstance(metadata, dict) else None
        forwarded = audit.get("forwarded_tokens") if isinstance(audit, dict) else None
        input_tokens = audit.get("input_tokens") if isinstance(audit, dict) else None
        if type(forwarded) is not int or forwarded > target * 2:
            return _soft_fail_sift_request(
                data, reason="sift_result_over_two_x_target", target=target,
                input_tokens=input_tokens if type(input_tokens) is int else None,
            )
        return result


class QwenContextRecovery(SiftLiteLLMHook):
    """Own bounded compaction admission for local Qwen requests."""

    def __init__(self) -> None:
        # Native Sift requires an explicit alias map. Configuration tests check
        # it against the authoritative deployment budget in litellm.yaml.
        super().__init__({"qwen3.8-27b": 32_768}, raw_limit=RAW_INTAKE_LIMIT)
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
    """Compact target requests toward 32K without imposing a route ceiling.

    A compacted result may use at most twice its selected Sift target. If Sift
    cannot meet that quality bound, preserve the caller's request and let the
    selected provider plus LiteLLM's normal fallback policy decide whether it
    fits their actual context limits. Only the 4M Sift intake bound is enforced
    here; it is not a provider context claim.
    """

    DEFAULT_TARGET = 32_768
    MAX_TARGET = 65_536
    RAW_LIMIT = RAW_INTAKE_LIMIT

    def __init__(self) -> None:
        super().__init__({"target": self.DEFAULT_TARGET}, raw_limit=self.RAW_LIMIT)

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                  data: dict, call_type: str) -> dict | None:
        from litellm.proxy import proxy_server

        model = data.get("model")
        if not isinstance(model, str):
            return None
        if model == "qwen3.8-27b":
            # Its earlier model-specific hook owns native capacity and audit.
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

        original = deepcopy(data)
        prepared = deepcopy(data)
        if response_input:
            if prepared.get("instructions") is not None and not isinstance(prepared["instructions"], str):
                return None
            try:
                counted = await asyncio.to_thread(
                    router._count_pre_call_check_tokens, messages=None,
                    input=prepared.get("input"), request_kwargs={**prepared, "model": model},
                )
            except Exception as error:
                logging.getLogger("LiteLLM Proxy").warning(
                    "sift_target_reminder_count_unavailable protocol=responses reason=%s",
                    type(error).__name__,
                )
                counted = None
        else:
            messages = prepared.get("messages")
            if not isinstance(messages, list):
                return None
            try:
                counted = await asyncio.to_thread(
                    router._count_pre_call_check_tokens, messages=messages, input=None,
                    request_kwargs={**prepared, "model": model},
                )
            except Exception as error:
                logging.getLogger("LiteLLM Proxy").warning(
                    "sift_target_reminder_count_unavailable protocol=messages reason=%s",
                    type(error).__name__,
                )
                counted = None

        if type(counted) is int and counted > 16_384:
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
                    instruction_end = 0
                    while (instruction_end < len(messages)
                           and messages[instruction_end].get("role") in {"system", "developer"}):
                        instruction_end += 1
                    messages.insert(instruction_end, {"role": "developer", "content": reminder})

        target = self.request_target(prepared, self.MAX_TARGET, default=self.DEFAULT_TARGET)
        if isinstance(original.get("metadata"), dict):
            original["metadata"].pop("sift_target_tokens", None)
        try:
            result = await self._compact(router, prepared, call_type=call_type, target=target,
                                         max_target=target * 2)
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {}
            sift_error = detail.get("error") if isinstance(detail.get("error"), dict) else {}
            reason = sift_error.get("reason") or sift_error.get("code") or f"sift_http_{error.status_code}"
            if reason == "raw_limit":
                # Four million is the bounded ingress/compaction limit. It is
                # independent of every provider's actual context capacity.
                raise
            return _soft_fail_sift_request(
                original, reason=str(reason), target=target,
                input_tokens=sift_error.get("input_tokens"),
            )

        if not isinstance(result, dict):
            return _soft_fail_sift_request(original, reason="no_compaction_result", target=target)
        metadata = result.get("litellm_metadata") or result.get("metadata") or {}
        audit = metadata.get("sift") if isinstance(metadata, dict) else None
        forwarded = audit.get("forwarded_tokens") if isinstance(audit, dict) else None
        input_tokens = audit.get("input_tokens") if isinstance(audit, dict) else None
        if type(forwarded) is not int or forwarded > target * 2:
            return _soft_fail_sift_request(
                original, reason="sift_result_over_two_x_target", target=target,
                input_tokens=input_tokens if type(input_tokens) is int else None,
            )
        return result

    async def async_post_call_response_headers_hook(
        self, data: dict, user_api_key_dict: Any, response: object,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        metadata = data.get("litellm_metadata") or data.get("metadata") or {}
        audit = metadata.get("sift") if isinstance(metadata, dict) else None
        if not isinstance(audit, dict) or type(audit.get("compacted")) is not bool:
            return None
        headers = {
            "x-sift-compacted": str(audit["compacted"]).lower(),
            "x-sift-estimated-input-tokens": str(audit.get("input_tokens", -1)),
            "x-sift-estimated-forwarded-tokens": str(audit.get("forwarded_tokens", -1)),
        }
        if audit.get("soft_failed") is True:
            headers["x-sift-soft-failed"] = "true"
        return headers


sift_qwen_hook = QwenContextRecovery()
sift_auto_hook = AutoContextRecovery()
sift_target_hook = TargetContextRecovery()
