"""Sift ownership for local Qwen and the gateway's automatic recovery route."""

from typing import Any

from fastapi import HTTPException
from sift.litellm_proxy import SiftLiteLLMHook


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
        return target

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
            return await self._compact(router, data, call_type=call_type, target=target)
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


sift_qwen_hook = SiftLiteLLMHook({"qwen3.8-27b": 65_536}, raw_limit=1_000_000)
sift_auto_hook = AutoContextRecovery()
