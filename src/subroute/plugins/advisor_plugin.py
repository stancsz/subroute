"""Opt-in Advisor tool injection for LiteLLM's Anthropic Messages endpoint."""

from __future__ import annotations

import os
import re
import uuid
import logging
import asyncio
import json
from typing import Any
from fastapi import HTTPException

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket

from subroute.handlers.codex_advisor import (
    call_codex_streaming_collect,
    has_tool_history,
    supports_tool_history,
)
from subroute.handlers.antigravity import invoke_agy, model_with_effort, prompt_from_messages
from subroute.handlers.antigravity import AntigravityBridgeError, AntigravityRequestTooLargeError
from subroute.handlers.codex_advisor import CodexAdvisorError


ADVISOR_TOOL_TYPE = "advisor_20260301"
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})
TOOL_HISTORY_ADVISORS = frozenset({
    "codex-gpt-6.1-sol-advisor",
    "codex-sol-advisor",
    "codex-astra-advisor",
    "codex-luna-advisor",
})
CODEX_SUBSCRIPTION_MODELS = frozenset({
    "codex-subscription", "codex-gpt-6.1-sol", "codex-astra", "codex-luna",
})
ADVISOR_MODEL_NAMES = {
    "codex-gpt-6.1-sol-advisor": "gpt-6.1-sol",
    "codex-sol-advisor": "gpt-6-sol",
    "codex-astra-advisor": "gpt-6-astra",
    "codex-luna-advisor": "gpt-6-luna",
}
ANTIGRAVITY_ADVISOR_MODELS = {
    "gemini-subscription": "gemini-3.8-flash",
    "gemini-subscription-3.7-flash": "gemini-3.7-flash",
    "gemini-subscription-3.6-flash": "gemini-3.6-flash",
    "gemini-subscription-pro": "gemini-3.1-pro",
}
ADVISOR_PROVIDER_FALLBACKS = {
    "gemini-subscription": "codex-gpt-6.1-sol-advisor",
    "codex-gpt-6.1-sol-advisor": "gemini-subscription",
}
ADVISOR_TOOL_MODEL_ALIASES = {
    # The public Gemini alias is also a target model, so advisor calls use a
    # private LiteLLM group to keep fallback policy scoped to consultation.
    "gemini-subscription": "gemini-3.8-flash-advisor",
}
logger = logging.getLogger(__name__)
_SECRET_FIELD = re.compile(
    r"(?i)\b(authorization|api[\s_-]?key|access[\s_-]?token|refresh[\s_-]?token|token|password)(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,;}\]]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_API_KEY_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})\b")
COMPACTION_INSTRUCTIONS = (
    "Create a concise factual briefing for an independent advisor. Do not answer the user's request. "
    "Preserve the user's objective, constraints, decisions, important evidence, unresolved questions, "
    "and tool results. Remove repetition and obsolete detail. Return only the briefing, with no preamble."
)
COMPACTION_MAX_OUTPUT_TOKENS = 8192
MINIMAX_COMPACTION_TIMEOUT = 30.0
LUNA_COMPACTION_TIMEOUT = 60.0


def _safe_error_reason(error: Exception) -> str:
    """Keep actionable provider context without logging credentials or newlines."""
    reason = str(error)
    reason = _BEARER_SECRET.sub("Bearer [REDACTED]", reason)
    reason = _SECRET_FIELD.sub(r"\1\2[REDACTED]", reason)
    reason = _API_KEY_TOKEN.sub("[REDACTED]", reason)
    reason = " ".join(reason.split())
    return reason[:300] or type(error).__name__


class AdvisorContextCompactionError(RuntimeError):
    def __init__(
        self, *, input_tokens: int, limit: int, target: int,
        attempts: list[dict[str, Any]],
    ) -> None:
        self.input_tokens = input_tokens
        self.limit = limit
        self.target = target
        self.attempts = attempts
        super().__init__("Advisor context exceeded its limit and bounded compaction failed")


class AdvisorPlugin(CustomLogger):
    """Enable advisor orchestration from the captured routing policy."""

    def __init__(
        self,
        *,
        max_uses: int = 3,
    ) -> None:
        super().__init__()
        if not 1 <= max_uses <= 5:
            raise ValueError("max_uses must be between 1 and LiteLLM's hard cap of 5")
        self.max_uses = max_uses

    @staticmethod
    def _context_limits(router: Any, model: str) -> tuple[int | None, int | None]:
        model_info = router.get_model_info(model) or {}
        advisor_limit = model_info.get("advisor_max_input_tokens")
        configured_limit, _ = router.get_configured_token_limits(model)
        limit = advisor_limit or configured_limit
        compact_limit = model_info.get("auto_compact_token_limit")
        if limit is not None and (
            type(compact_limit) is not int or compact_limit <= 0 or compact_limit >= limit
        ):
            compact_limit = int(limit * 0.8)
        return limit, compact_limit

    @staticmethod
    def _compaction_error_status(exc: AdvisorContextCompactionError) -> int:
        statuses = [attempt.get("status") for attempt in exc.attempts]
        if statuses and statuses[-1] == "over_budget":
            return 413
        if any("timed out" in str(attempt.get("reason", "")).lower() for attempt in exc.attempts):
            return 504
        return 502

    @staticmethod
    async def _count_context_tokens(router: Any, data: dict, messages: list[dict[str, Any]]) -> int:
        request_kwargs = {**data, "messages": messages}
        return await asyncio.to_thread(
            router._count_pre_call_check_tokens,
            messages=messages,
            input=data.get("input"),
            request_kwargs=request_kwargs,
        )

    @staticmethod
    def _contains_media(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("type") in {
                "image", "image_url", "input_image", "audio", "input_audio",
                "output_audio", "video", "file",
            }:
                return True
            return any(AdvisorPlugin._contains_media(item) for item in value.values())
        if isinstance(value, list):
            return any(AdvisorPlugin._contains_media(item) for item in value)
        return isinstance(value, str) and ("data:image/" in value or "data:audio/" in value)

    @staticmethod
    def _summary_messages(
        messages: list[dict[str, Any]], summary: str,
    ) -> list[dict[str, Any]]:
        latest_user = AdvisorPlugin._latest_user_index(messages)
        if latest_user < 0:
            raise ValueError("Advisor context compaction requires a user message")
        result: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index == latest_user:
                result.append({
                    "role": "assistant",
                    "content": f"Relevant prior context, compacted for this advisor call:\n{summary}",
                })
            if index < latest_user and message.get("role") not in {"system", "developer"}:
                continue
            result.append(message)
        return result

    @staticmethod
    def _latest_user_index(messages: list[dict[str, Any]]) -> int:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list) and content and all(
                isinstance(part, dict) and part.get("type") == "tool_result"
                for part in content
            ):
                continue
            return index
        return -1

    @staticmethod
    def _completion_text(response: Any) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("Compactor returned no completion choice")
        choice = choices[0]
        if getattr(choice, "finish_reason", None) in {"length", "content_filter"}:
            raise RuntimeError("Compactor response did not complete normally")
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts = [
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            ]
            result = "\n".join(part for part in text_parts if isinstance(part, str)).strip()
            if result:
                return result
        raise RuntimeError("Compactor returned no text briefing")

    @staticmethod
    def _usage_receipt(usage: Any) -> dict[str, Any]:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if (
            type(prompt_tokens) is int and prompt_tokens >= 0
            and type(completion_tokens) is int and completion_tokens >= 0
        ):
            return {
                "usage_source": "provider",
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            }
        return {"usage_source": "unavailable", "input_tokens": None, "output_tokens": None}

    @staticmethod
    async def _minimax_compaction(router: Any, messages: list[dict[str, Any]]) -> tuple[str, Any]:
        transcript = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        response = await router.acompletion(
            model="minimax",
            messages=[
                {"role": "system", "content": COMPACTION_INSTRUCTIONS},
                {"role": "user", "content": transcript},
            ],
            max_tokens=COMPACTION_MAX_OUTPUT_TOKENS,
            timeout=MINIMAX_COMPACTION_TIMEOUT,
            num_retries=0,
            disable_fallbacks=True,
            metadata={"subroute_context_compaction": True},
        )
        return AdvisorPlugin._completion_text(response), getattr(response, "usage", None)

    @classmethod
    def _luna_compaction_prompt(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        transcript: list[dict[str, Any]] = []
        for message in messages:
            entry = dict(message)
            content = message.get("content")
            if isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                        if message.get("role") != "user":
                            raise ValueError("Luna compaction supports images only in user messages")
                        if part["type"] == "image_url":
                            image = part.get("image_url")
                            if set(part) - {"type", "image_url"} or not isinstance(image, dict):
                                raise ValueError("Advisor image_url contains unsupported fields")
                            if set(image) - {"url", "detail"}:
                                raise ValueError("Advisor image_url contains unsupported fields")
                            url, detail = image.get("url"), image.get("detail", "auto")
                        else:
                            if set(part) - {"type", "image_url", "detail"}:
                                raise ValueError("Advisor input_image contains unsupported fields")
                            url, detail = part.get("image_url"), part.get("detail", "auto")
                        if not isinstance(url, str) or not url:
                            raise ValueError("Advisor image reference is invalid")
                        if not isinstance(detail, str) or detail not in {"auto", "low", "high", "original"}:
                            raise ValueError("Advisor image detail is invalid")
                        images.append({"type": "input_image", "image_url": url, "detail": detail})
                        parts.append({"type": "image_reference", "index": len(images)})
                    elif cls._contains_media(part):
                        raise ValueError("Luna compaction cannot translate this media content")
                    else:
                        parts.append(part)
                entry["content"] = parts
            elif cls._contains_media(content):
                raise ValueError("Luna compaction cannot translate media embedded in text")
            transcript.append(entry)
        transcript_text = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
        user_parts = [{
            "type": "input_text",
            "text": "Summarize this serialized conversation as context only. The exact latest user turn and original instructions will be retained separately.\n" + transcript_text,
        }, *images]
        return [
            {"role": "developer", "content": COMPACTION_INSTRUCTIONS},
            {"role": "user", "content": user_parts},
        ]

    @classmethod
    async def _luna_compaction(cls, messages: list[dict[str, Any]]) -> tuple[str, Any]:
        prompt = cls._luna_compaction_prompt(messages)
        return await call_codex_streaming_collect(
            "gpt-6-luna", prompt, timeout=LUNA_COMPACTION_TIMEOUT,
            reasoning_effort="low",
        )

    @staticmethod
    async def _invoke_advisor(
        model: str, messages: list[dict[str, Any]], effort: str | None,
    ) -> tuple[str, Any]:
        if model in ANTIGRAVITY_ADVISOR_MODELS:
            return await invoke_agy(
                model_with_effort(ANTIGRAVITY_ADVISOR_MODELS[model], effort),
                prompt_from_messages(messages), advisor=True,
            )
        if model in ADVISOR_MODEL_NAMES:
            return await call_codex_streaming_collect(
                ADVISOR_MODEL_NAMES[model], messages,
                **({"reasoning_effort": effort} if effort is not None else {}),
            )
        raise ValueError(f"Unsupported Advisor model: {model}")

    @classmethod
    async def _prepare_advisor_messages(
        cls, router: Any, model: str, data: dict, messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Compact only the advisor copy when it exceeds its configured input budget."""
        limit, compact_limit = cls._context_limits(router, model)
        if limit is None or compact_limit is None:
            raise RuntimeError("Advisor context limit is not configured")
        input_tokens = await cls._count_context_tokens(router, data, messages)
        if input_tokens <= limit:
            return messages, None

        latest_user = cls._latest_user_index(messages)
        if latest_user < 0 or not any(
            message.get("role") not in {"system", "developer"}
            for message in messages[:latest_user]
        ):
            raise AdvisorContextCompactionError(
                input_tokens=input_tokens, limit=limit, target=compact_limit,
                attempts=[{
                    "model": "none", "status": "over_budget",
                    "reason": "no_compactable_prior_turns",
                }],
            )

        attempts: list[dict[str, Any]] = []
        compacted_messages: list[dict[str, Any]] | None = None
        summary: str | None = None
        if cls._contains_media(messages):
            attempts.append({
                "model": "minimax", "status": "skipped",
                "reason": "unsupported_multimodal_input",
            })
        else:
            try:
                summary, usage = await cls._minimax_compaction(router, messages)
                compacted_messages = cls._summary_messages(messages, summary)
                compacted_tokens = await cls._count_context_tokens(router, data, compacted_messages)
                attempt = {
                    "model": "minimax", "status": "succeeded" if compacted_tokens <= compact_limit else "over_budget",
                    **cls._usage_receipt(usage), "estimated_context_tokens": compacted_tokens,
                }
                attempts.append(attempt)
                if compacted_tokens <= compact_limit:
                    return compacted_messages, {
                        "status": "compacted", "provider": "minimax",
                        "original_estimated_tokens": input_tokens,
                        "final_estimated_tokens": compacted_tokens,
                        "target_tokens": compact_limit, "attempts": attempts,
                    }
            except Exception as exc:
                attempts.append({
                    "model": "minimax", "status": "failed",
                    "reason": _safe_error_reason(exc),
                })

        try:
            summary, usage = await cls._luna_compaction(messages)
            compacted_messages = cls._summary_messages(messages, summary)
            compacted_tokens = await cls._count_context_tokens(router, data, compacted_messages)
            attempt = {
                "model": "codex-luna-advisor",
                "status": "succeeded" if compacted_tokens <= compact_limit else "over_budget",
                **cls._usage_receipt(usage), "estimated_context_tokens": compacted_tokens,
            }
            attempts.append(attempt)
            if compacted_tokens <= compact_limit:
                return compacted_messages, {
                    "status": "compacted", "provider": "codex-luna-advisor",
                    "original_estimated_tokens": input_tokens,
                    "final_estimated_tokens": compacted_tokens,
                    "target_tokens": compact_limit, "attempts": attempts,
                }
        except Exception as exc:
            attempts.append({
                "model": "codex-luna-advisor", "status": "failed",
                "reason": _safe_error_reason(exc),
            })

        raise AdvisorContextCompactionError(
            input_tokens=input_tokens, limit=limit, target=compact_limit, attempts=attempts,
        )


    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict | None:
        """Consult the selected advisor on Messages requests; an empty selection disables it."""

        _, metadata = get_or_create_metadata_bucket(data)
        if metadata.get("advisor_sub_call") is True:
            from litellm.proxy import proxy_server

            router = proxy_server.llm_router
            if router is None:
                raise HTTPException(status_code=503, detail="Advisor context limit is unavailable")
            try:
                messages, compaction = await self._prepare_advisor_messages(
                    router, data.get("model", ""), data, data.get("messages") or [],
                )
            except AdvisorContextCompactionError as exc:
                metadata["gateway_advisor_compaction"] = {
                    "status": "failed", "original_estimated_tokens": exc.input_tokens,
                    "limit_tokens": exc.limit, "target_tokens": exc.target,
                    "attempts": exc.attempts,
                }
                raise HTTPException(
                    status_code=self._compaction_error_status(exc),
                    detail={"error": {
                        "type": "advisor_context_compaction_failed",
                        "message": "Advisor context exceeded its limit; bounded compaction failed",
                        "request_id": data.get("litellm_call_id"),
                        "compaction": metadata["gateway_advisor_compaction"],
                    }},
                ) from exc
            if compaction is not None:
                metadata["gateway_advisor_compaction"] = compaction
                data["messages"] = messages
                logger.warning(
                    "advisor context compacted request_id=%s model=%s provider=%s original_estimated_tokens=%s final_estimated_tokens=%s attempts=%s",
                    data.get("litellm_call_id"), data.get("model"), compaction["provider"],
                    compaction["original_estimated_tokens"], compaction["final_estimated_tokens"],
                    compaction["attempts"],
                )
                return data
            return None
        if call_type not in SUPPORTED_CALL_TYPES:
            return None
        if data.get("gateway_image_request") is not None:
            # The image intent exception owns this request; do not add a
            # saved advisor call, instructions, or provider spending.
            return None
        # Native advisor calls retain metadata. Never turn a consultation into
        # another consultation, including Gemini aliases shared with targets.
        policy = metadata.get("gateway_policy")
        if not isinstance(policy, dict):
            return None
        advisor_model = policy.get("advisor_model")
        if not advisor_model:
            return None
        advisor_effort = policy.get("advisor_reasoning_effort")

        messages = data.get("messages") or []
        if has_tool_history(messages) and (
            advisor_model not in TOOL_HISTORY_ADVISORS
            or not supports_tool_history(messages)
        ):
            metadata["gateway_advisor"] = {
                "status": "skipped",
                "reason": "unsupported_tool_history",
                "model": advisor_model,
            }
            return data

        minimax_gemini_pair = (
            data.get("model") == "minimax"
            and advisor_model in ANTIGRAVITY_ADVISOR_MODELS
        )
        existing_preconsult_pair = (
            data.get("model") in CODEX_SUBSCRIPTION_MODELS | ANTIGRAVITY_ADVISOR_MODELS.keys()
            and advisor_model in {*ADVISOR_MODEL_NAMES, *ANTIGRAVITY_ADVISOR_MODELS}
        )
        if (
            (minimax_gemini_pair or existing_preconsult_pair)
            and advisor_model in ANTIGRAVITY_ADVISOR_MODELS
            and any(not isinstance(message.get("content", ""), str) for message in messages)
        ):
            metadata["gateway_advisor"] = {
                "status": "skipped",
                "reason": "unsupported_content",
                "model": advisor_model,
            }
            return data
        if minimax_gemini_pair or existing_preconsult_pair:
            # LiteLLM's router checks run after proxy callbacks. This earlier
            # consultation must use that same counter and configured cap before
            # spending provider tokens. Retire when LiteLLM checks before hooks.
            from litellm import ContextWindowExceededError
            from litellm.proxy import proxy_server

            router = proxy_server.llm_router
            consultation_id = uuid.uuid4().hex
            advisor_messages = messages
            resolved_advisor_model = advisor_model
            fallback_attempt: dict[str, str] | None = None
            try:
                if router is None:
                    raise RuntimeError("Advisor context limit is unavailable")
                target_limit, _ = router.get_configured_token_limits(data["model"])
                original_tokens = await self._count_context_tokens(router, data, messages)
                if target_limit is not None and original_tokens > target_limit:
                    raise ContextWindowExceededError(
                        message=f"Max Input Tokens={target_limit}, Got={original_tokens}; advisor was not called",
                        model=data["model"], llm_provider="subroute",
                    )
                advisor_messages, compaction = await self._prepare_advisor_messages(
                    router, advisor_model, data, messages,
                )
                if compaction is not None:
                    metadata["gateway_advisor_compaction"] = compaction
                    logger.warning(
                        "advisor context compacted request_id=%s consultation_id=%s model=%s provider=%s original_estimated_tokens=%s final_estimated_tokens=%s attempts=%s",
                        data.get("litellm_call_id"), consultation_id, advisor_model,
                        compaction["provider"], compaction["original_estimated_tokens"],
                        compaction["final_estimated_tokens"], compaction["attempts"],
                    )
                try:
                    advice, usage = await self._invoke_advisor(
                        advisor_model, advisor_messages, advisor_effort,
                    )
                except Exception as first_error:
                    fallback_model = ADVISOR_PROVIDER_FALLBACKS.get(advisor_model)
                    if fallback_model is None:
                        raise
                    fallback_attempt = {
                        "failed_model": advisor_model,
                        "reason": _safe_error_reason(first_error),
                    }
                    logger.warning(
                        "advisor fallback request_id=%s consultation_id=%s failed_model=%s fallback_model=%s reason=%s",
                        data.get("litellm_call_id"), consultation_id, advisor_model,
                        fallback_model, fallback_attempt["reason"],
                    )
                    advice, usage = await self._invoke_advisor(
                        fallback_model, advisor_messages, advisor_effort,
                    )
                    resolved_advisor_model = fallback_model
            except ContextWindowExceededError:
                raise
            except Exception as exc:
                reason = _safe_error_reason(exc)
                gateway_request_id = data.get("litellm_call_id")
                metadata["gateway_advisor"] = {
                    "status": "failed", "model": advisor_model,
                    "consultation_id": consultation_id, "reason": reason,
                    "gateway_request_id": gateway_request_id,
                    **({"fallback_attempt": fallback_attempt} if fallback_attempt else {}),
                }
                if isinstance(exc, AdvisorContextCompactionError):
                    metadata["gateway_advisor_compaction"] = {
                        "status": "failed",
                        "original_estimated_tokens": exc.input_tokens,
                        "limit_tokens": exc.limit,
                        "target_tokens": exc.target,
                        "attempts": exc.attempts,
                    }
                    logger.error(
                        "advisor context compaction failed request_id=%s advisor=%s original_estimated_tokens=%s limit_tokens=%s target_tokens=%s attempts=%s",
                        gateway_request_id, advisor_model, exc.input_tokens, exc.limit,
                        exc.target, exc.attempts,
                    )
                logger.error(
                    "advisor consultation failed gateway_request_id=%s consultation_id=%s model=%s base_model=%s error_type=%s error=%s",
                    gateway_request_id, consultation_id, advisor_model,
                    data.get("model"), type(exc).__name__, reason,
                )
                if isinstance(exc, AdvisorContextCompactionError):
                    status_code = self._compaction_error_status(exc)
                elif isinstance(exc, AntigravityRequestTooLargeError):
                    status_code = 413
                elif isinstance(exc, ValueError):
                    status_code = 400
                elif isinstance(exc, TimeoutError):
                    status_code = 504
                elif isinstance(exc, (AntigravityBridgeError, CodexAdvisorError)):
                    upstream_status = exc.status_code
                    status_code = upstream_status if upstream_status == 429 else (
                        504 if upstream_status == 504 else 502
                    )
                else:
                    status_code = 502
                error_type = (
                    "advisor_context_compaction_failed"
                    if isinstance(exc, AdvisorContextCompactionError)
                    else "advisor_consultation_failed"
                )
                error_message = (
                    "Advisor context exceeded its limit; bounded compaction failed; base request was not sent"
                    if isinstance(exc, AdvisorContextCompactionError)
                    else "Selected advisor failed; base request was not sent"
                )
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "error": {
                            "type": error_type,
                            "message": error_message,
                            "request_id": gateway_request_id,
                            "consultation_id": consultation_id,
                            **({"compaction": metadata["gateway_advisor_compaction"]}
                               if isinstance(exc, AdvisorContextCompactionError) else {}),
                        }
                    },
                ) from exc
            data["messages"] = [
                *messages,
                {"role": "developer", "content": f"Independent advisor guidance:\n{advice}"},
            ]
            metadata["gateway_advisor"] = {
                "status": "advice_injected", "model": advisor_model,
                "resolved_model": resolved_advisor_model,
                "consultation_id": consultation_id, "usage_source": "provider",
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "reasoning_effort": advisor_effort,
                **({"fallback_attempt": fallback_attempt} if fallback_attempt else {}),
            }
            if metadata.get("gateway_advisor_compaction"):
                metadata["gateway_advisor"]["compaction"] = metadata["gateway_advisor_compaction"]
            logger.warning(
                "advisor advice_injected consultation_id=%s model=%s input_tokens=%s output_tokens=%s base_model=%s reasoning_effort=%s",
                consultation_id, advisor_model, usage.prompt_tokens,
                usage.completion_tokens, data["model"], advisor_effort,
            )
            return data

        raw_tools = data.get("tools")
        if raw_tools is None:
            tools: list[dict[str, Any]] = []
        elif isinstance(raw_tools, list) and all(
            isinstance(tool, dict) for tool in raw_tools
        ):
            tools = list(raw_tools)
        else:
            raise ValueError("tools must be a list of objects")

        if not any(tool.get("type") == ADVISOR_TOOL_TYPE for tool in tools):
            tools.append(
                {
                    "type": ADVISOR_TOOL_TYPE,
                    "name": "advisor",
                    "model": ADVISOR_TOOL_MODEL_ALIASES.get(advisor_model, advisor_model),
                    "max_uses": self.max_uses,
                    "caching": {"type": "ephemeral", "ttl": "5m"},
                }
            )
            data["tools"] = tools
        return data


advisor_plugin_instance = AdvisorPlugin(
    max_uses=int(os.getenv("ADVISOR_MAX_USES", "3")),
)
