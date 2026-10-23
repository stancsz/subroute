"""Apply the saved Advisor policy across LiteLLM chat, Responses, and Messages."""

from __future__ import annotations

import os
import re
import uuid
import logging
import asyncio
import json
from collections import OrderedDict
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
from subroute.advisor_analytics import AdvisorAnalytics
from subroute.advisor_frequency import AdvisorFrequencyGate
from subroute.advisor_ranker import AdvisorRequestRanker, AdvisorRankerUnavailable


ADVISOR_TOOL_TYPE = "advisor_20260301"
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})
MESSAGE_CALL_TYPES = frozenset({"completion", "acompletion", *SUPPORTED_CALL_TYPES})
RESPONSES_CALL_TYPES = frozenset({"responses", "aresponses"})
ADVISOR_CALL_TYPES = MESSAGE_CALL_TYPES | RESPONSES_CALL_TYPES
TOOL_HISTORY_ADVISORS = frozenset({
    "codex-gpt-6.1-sol-advisor",
    "codex-sol-advisor",
    "codex-astra-advisor",
    "codex-luna-advisor",
})
ADVISOR_MODEL_NAMES = {
    "codex-gpt-6.1-sol-advisor": "gpt-6.1-sol",
    "codex-sol-advisor": "gpt-6-sol",
    "codex-astra-advisor": "gpt-6-astra",
    "codex-luna-advisor": "gpt-6-luna",
}
ANTIGRAVITY_ADVISOR_MODELS = {
    "gemini-subscription": "gemini-3.8-flash",
    "gemini-subscription-argon": "gemini-4-argon",
    "gemini-subscription-3.7-flash": "gemini-3.7-flash",
    "gemini-subscription-3.6-flash": "gemini-3.6-flash",
    "gemini-subscription-pro": "gemini-3.1-pro",
}
ADVISOR_PROVIDER_FALLBACKS = {
    "gemini-subscription": "codex-gpt-6.1-sol-advisor",
    "codex-gpt-6.1-sol-advisor": "gemini-subscription",
}
ADVISOR_BRIEF_INSTRUCTIONS = (
    "Advise the calling model on the current task. Give only the most useful correction, "
    "risk, or next step, grounded in the supplied context. Use at most three short bullets "
    "and 120 words. Do not repeat the request, narrate your reasoning, or write the final "
    "answer. If the approach is sound, say so briefly."
)
SUPPLEMENTAL_ADVISOR_MODEL = "claude-opus-5-5-low-advisor"
SUPPLEMENTAL_ADVISOR_PROVIDER_MODEL = "claude-opus-5-5-low"
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
ADVISOR_FORWARD_LIMIT = 16_384
COMPACTION_FORWARD_LIMIT = 32_768
MINIMAX_COMPACTION_TIMEOUT = 30.0
MIMO_COMPACTION_TIMEOUT = 30.0


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

    def __init__(self) -> None:
        super().__init__()
        self.analytics = AdvisorAnalytics()
        self.request_ranker = AdvisorRequestRanker()
        self.frequency_gate = AdvisorFrequencyGate()
        self._ranked_attempts: OrderedDict[str, None] = OrderedDict()

    async def _record_analytics(self, metadata: dict, **markers) -> None:
        request_id = metadata.get("_gateway_analytics_id")
        if request_id and metadata.get("_gateway_analytics_ok") is True:
            metadata["_gateway_analytics_ok"] = await self.analytics.record(request_id, **markers)

    @staticmethod
    def _frequency_policy_key(policy: dict) -> str:
        return json.dumps({key: policy.get(key) for key in (
            "policy_version", "advisor_model", "advisor_sensitivity",
        )}, sort_keys=True)

    def frequency_state(self, policy: dict) -> dict:
        rates = {"conservative": 0.3, "balanced": 0.5, "frequent": 0.7}
        return {
            "classifier": "ranked",
            "policy_version": policy.get("policy_version"),
            "target_rate": rates.get(policy.get("advisor_sensitivity", "balanced")),
            "scope": "process_policy",
            **self.frequency_gate.snapshot(self._frequency_policy_key(policy)),
        }

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
    def _responses_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Project supported Responses input into advisor messages without mutating it."""
        if data.get("previous_response_id"):
            raise ValueError("Responses advisor context cannot resolve previous_response_id")
        messages: list[dict[str, Any]] = []
        instructions = data.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise ValueError("Responses instructions must be text for advisor context")
            if instructions:
                messages.append({"role": "developer", "content": instructions})
        source = data.get("input")
        if isinstance(source, str):
            return [*messages, {"role": "user", "content": source}]
        if not isinstance(source, list):
            raise ValueError("Responses input must be text or a list for advisor context")
        calls: set[str] = set()
        for item in source:
            if not isinstance(item, dict):
                raise ValueError("Responses input items must be objects for advisor context")
            item_type = item.get("type", "message")
            if item_type == "message":
                role = item.get("role")
                if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant"}:
                    raise ValueError("Responses message role is unsupported for advisor context")
                content = item.get("content", "")
                if isinstance(content, str):
                    messages.append({"role": "developer" if role == "system" else role, "content": content})
                    continue
                if not isinstance(content, list):
                    raise ValueError("Responses message content is unsupported for advisor context")
                parts: list[dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        raise ValueError("Responses content parts must be objects for advisor context")
                    part_type = part.get("type")
                    if part_type in {"input_text", "output_text"} and isinstance(part.get("text"), str):
                        parts.append({"type": "text", "text": part["text"]})
                    elif part_type == "input_image" and isinstance(part.get("image_url"), str):
                        parts.append({"type": "image_url", "image_url": {
                            "url": part["image_url"], "detail": part.get("detail", "auto"),
                        }})
                    else:
                        raise ValueError("Responses content type is unsupported for advisor context")
                messages.append({"role": "developer" if role == "system" else role, "content": parts})
            elif item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments", "{}")
                if not isinstance(call_id, str) or not call_id or call_id in calls:
                    raise ValueError("Responses function call id is invalid for advisor context")
                if not isinstance(name, str) or not name or not isinstance(arguments, (str, dict)):
                    raise ValueError("Responses function call is invalid for advisor context")
                calls.add(call_id)
                messages.append({"role": "assistant", "tool_calls": [{
                    "id": call_id, "type": "function", "function": {
                        "name": name, "arguments": arguments,
                    },
                }]})
            elif item_type == "function_call_output":
                call_id, output = item.get("call_id"), item.get("output")
                if not isinstance(call_id, str) or call_id not in calls or not isinstance(output, str):
                    raise ValueError("Responses function output has no supported matching call")
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
            else:
                raise ValueError("Responses input item is unsupported for advisor context")
        if not any(message.get("role") == "user" for message in messages):
            raise ValueError("Responses advisor context requires a user message")
        return messages

    @classmethod
    def _request_messages(cls, data: dict[str, Any], call_type: str) -> list[dict[str, Any]]:
        if call_type in RESPONSES_CALL_TYPES:
            return cls._responses_messages(data)
        messages = data.get("messages")
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise ValueError("Advisor request messages must be a list of objects")
        if call_type in {"completion", "acompletion"}:
            messages = [
                {**message, "role": "developer"} if message.get("role") == "system" else message
                for message in messages
            ]
        return messages

    @staticmethod
    async def _count_context_tokens(router: Any, data: dict, messages: list[dict[str, Any]]) -> int:
        request_kwargs = {key: value for key, value in data.items() if key not in {"input", "instructions"}}
        request_kwargs["messages"] = messages
        return await asyncio.to_thread(
            router._count_pre_call_check_tokens,
            messages=messages,
            input=None,
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
    def _contains_non_image_media(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("type") in {"audio", "input_audio", "output_audio", "video", "file"}:
                return True
            if value.get("type") in {"image_url", "input_image"}:
                return False
            return any(AdvisorPlugin._contains_non_image_media(item) for item in value.values())
        if isinstance(value, list):
            return any(AdvisorPlugin._contains_non_image_media(item) for item in value)
        return isinstance(value, str) and ("data:audio/" in value or "data:video/" in value)

    @staticmethod
    def _summary_messages(
        messages: list[dict[str, Any]], summary: str,
    ) -> list[dict[str, Any]]:
        from copy import deepcopy
        latest_user = AdvisorPlugin._latest_user_index(messages)
        if latest_user < 0:
            raise ValueError("Advisor context compaction requires a user message")
        prior_images = []
        for message in messages[:latest_user]:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            images = [deepcopy(part) for part in content
                      if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}]
            if images:
                preserved = {key: deepcopy(value) for key, value in message.items() if key != "content"}
                preserved["content"] = images
                prior_images.append(preserved)
        result: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index == latest_user:
                result.append({
                    "role": "assistant",
                    "content": f"Relevant prior context, compacted for this advisor call:\n{summary}",
                })
                result.extend(prior_images)
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

    @classmethod
    def _latest_user_text(cls, messages: list[dict[str, Any]]) -> str:
        latest_user = cls._latest_user_index(messages)
        if latest_user < 0:
            return ""
        content = messages[latest_user].get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
                and isinstance(part.get("text"), str)
            )
        return ""

    @staticmethod
    def _brief_advisor_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Add direction only to the gateway's preconsultation copy. The target
        # keeps its exact conversation and tool ordering.
        return [{"role": "developer", "content": ADVISOR_BRIEF_INSTRUCTIONS}, *messages]

    async def _ranked_decision(
        self, messages: list[dict[str, Any]], *, request_id: str, policy: dict,
    ) -> dict[str, Any]:
        try:
            score = await self.request_ranker.rank(self._latest_user_text(messages))
        except AdvisorRankerUnavailable as exc:
            logger.error("advisor ranker unavailable request_id=%s error_type=%s", request_id, type(exc).__name__)
            raise HTTPException(status_code=503, detail="Advisor ranker unavailable; base request was not sent") from exc
        decision = self.frequency_gate.choose(
            request_id, score, {"conservative": 0.3, "balanced": 0.5, "frequent": 0.7}[policy.get("advisor_sensitivity", "balanced")],
            self._frequency_policy_key(policy),
        )
        return {**decision, "source": "ranked", "classifier_model": "multilingual-minilm-logistic-v1"}

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
        from subroute.usage_accounting import accounting_role
        transcript = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        prompt = await AdvisorPlugin._compact_request_for_dispatch(router, "minimax", [
            {"role": "system", "content": COMPACTION_INSTRUCTIONS},
            {"role": "user", "content": transcript},
        ])
        with accounting_role("compaction"):
            response = await router.acompletion(
                model="minimax",
                messages=prompt,
                max_tokens=COMPACTION_MAX_OUTPUT_TOKENS,
                timeout=MINIMAX_COMPACTION_TIMEOUT,
                num_retries=0,
                disable_fallbacks=True,
                metadata={"subroute_context_compaction": True},
            )
        return AdvisorPlugin._completion_text(response), getattr(response, "usage", None)

    @classmethod
    def _mimo_compaction_prompt(
        cls, messages: list[dict[str, Any]], *, multimodal: bool = False,
    ) -> list[dict[str, Any]]:
        if not multimodal:
            transcript = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            return [
                {"role": "system", "content": COMPACTION_INSTRUCTIONS},
                {"role": "user", "content": transcript},
            ]
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
                            raise ValueError("MiMo compaction supports images only in user messages")
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
                        images.append({"type": "image_url", "image_url": {"url": url, "detail": detail}})
                        parts.append({
                            "type": "text",
                            "text": f"[image attachment {len(images)} retained separately]",
                        })
                    elif cls._contains_media(part):
                        raise ValueError("MiMo compaction cannot translate this media content")
                    else:
                        parts.append(part)
                entry["content"] = parts
            elif cls._contains_media(content):
                raise ValueError("MiMo compaction cannot translate media embedded in text")
            transcript.append(entry)
        transcript_text = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
        user_parts = [{
            "type": "text",
            "text": "Summarize this serialized conversation as context only. The exact latest user turn and original instructions will be retained separately.\n" + transcript_text,
        }, *images]
        return [
            {"role": "system", "content": COMPACTION_INSTRUCTIONS},
            {"role": "user", "content": user_parts},
        ]

    @classmethod
    async def _mimo_compaction(
        cls, router: Any, messages: list[dict[str, Any]], *, multimodal: bool = False,
    ) -> tuple[str, Any]:
        from subroute.usage_accounting import accounting_role
        prompt = cls._mimo_compaction_prompt(messages, multimodal=multimodal)
        prompt = await cls._compact_request_for_dispatch(
            router, "mimo-v2.5-pro" if multimodal else "mimo-v2.6-flash", prompt,
        )
        with accounting_role("compaction"):
            response = await router.acompletion(
                model="mimo-v2.5-pro" if multimodal else "mimo-v2.6-flash",
                messages=prompt,
                max_tokens=COMPACTION_MAX_OUTPUT_TOKENS,
                timeout=MIMO_COMPACTION_TIMEOUT,
                num_retries=0,
                disable_fallbacks=True,
                metadata={"subroute_context_compaction": True},
            )
        return cls._completion_text(response), getattr(response, "usage", None)

    @classmethod
    async def _compact_request_for_dispatch(
        cls, router: Any, model: str, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Bound direct text-compaction input via Sift, then recount at dispatch."""
        data = {"model": model, "messages": messages}
        messages, _ = await cls._sift_compaction(
            router, data, messages, COMPACTION_FORWARD_LIMIT,
        )
        final_count = await cls._count_context_tokens(router, data, messages)
        if final_count > COMPACTION_FORWARD_LIMIT:
            raise ValueError("Text compaction request exceeds the 32768-token forwarding limit")
        return messages

    @staticmethod
    async def _sift_compaction(
        router: Any, data: dict[str, Any], messages: list[dict[str, Any]], target: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run Sift on text while retaining and counting opaque user image blocks."""
        from copy import deepcopy
        from sift.core import compact_messages

        latest_user = AdvisorPlugin._latest_user_index(messages)
        latest_original = deepcopy(messages[latest_user]) if latest_user >= 0 else None
        image_messages: list[tuple[int, dict[str, Any]]] = []
        sift_messages: list[dict[str, Any]] = []
        image_number = 0
        for message in messages:
            entry = deepcopy(message)
            content = message.get("content")
            if isinstance(content, list):
                text_parts: list[Any] = []
                images: list[dict[str, Any]] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                        if message.get("role") != "user":
                            raise ValueError("Sift supports retained advisor images only in user messages")
                        image_number += 1
                        images.append(deepcopy(part))
                        text_parts.append({
                            "type": "text",
                            "text": f"[image attachment {image_number} retained separately]",
                        })
                    elif AdvisorPlugin._contains_media(part):
                        raise ValueError("Sift cannot compact this advisor media content")
                    else:
                        text_parts.append(part)
                if images:
                    preserved = {key: deepcopy(value) for key, value in message.items() if key != "content"}
                    preserved["content"] = images
                    image_messages.append((len(sift_messages), preserved))
                entry["content"] = text_parts
            elif AdvisorPlugin._contains_media(content):
                raise ValueError("Sift cannot compact media embedded in advisor text")
            sift_messages.append(entry)

        counting_data = {
            key: value for key, value in data.items()
            if key not in {"input", "instructions", "messages"}
        }

        def restore(candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
            restored = deepcopy(candidate)
            candidate_latest = AdvisorPlugin._latest_user_index(restored)
            if candidate_latest < 0:
                # Core budget probes include instruction-only prefixes. They
                # are counting candidates rather than dispatchable requests.
                return restored
            if latest_original is not None:
                restored[candidate_latest] = deepcopy(latest_original)
            # The exact latest user message already contains its images. Restore
            # only older image blocks separately so they survive text extraction
            # without duplicating images from the active turn.
            historical_images = [
                message for index, message in image_messages if index != latest_user
            ]
            restored[candidate_latest:candidate_latest] = deepcopy(historical_images)
            return restored

        def count(candidate: list[dict[str, Any]]) -> int:
            restored = restore(candidate)
            request_kwargs = {**counting_data, "messages": restored}
            return router._count_pre_call_check_tokens(
                messages=restored, input=None, request_kwargs=request_kwargs,
            )

        result = await asyncio.to_thread(
            compact_messages, sift_messages, count, target=target, protocol="openai",
        )
        restored = restore(result.messages)
        if AdvisorPlugin._latest_user_index(restored) < 0:
            raise ValueError("Sift compaction lost the current user turn")
        return restored, result.audit

    @staticmethod
    async def _invoke_advisor(
        model: str, messages: list[dict[str, Any]], effort: str | None,
    ) -> tuple[str, Any]:
        from subroute.usage_accounting import direct_attempt, observe_direct
        provider = "antigravity" if model in ANTIGRAVITY_ADVISOR_MODELS else "codex-subscription"
        async with direct_attempt(model, provider, "advisor"):
            result = await AdvisorPlugin._invoke_advisor_transport(model, messages, effort)
            await observe_direct({"usage": result[1]})
            return result

    @classmethod
    async def _check_advisor_dispatch(
        cls, router: Any, model: str, data: dict, messages: list[dict[str, Any]],
    ) -> int:
        """Recount the exact advisor copy immediately before each provider dispatch."""
        count = await cls._count_context_tokens(router, {**data, "model": model}, messages)
        if count > ADVISOR_FORWARD_LIMIT:
            raise ValueError("Advisor request exceeds the 16384-token forwarding limit")
        return count

    @staticmethod
    async def _invoke_advisor_transport(
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
        count_data = {**data, "model": model}
        input_tokens = await cls._count_context_tokens(router, count_data, messages)
        limit = min(limit, ADVISOR_FORWARD_LIMIT)
        compact_limit = min(compact_limit, ADVISOR_FORWARD_LIMIT)
        if input_tokens <= limit:
            return messages, None

        attempts: list[dict[str, Any]] = []
        compaction_source = messages
        sift_assisted = False
        try:
            compaction_source, sift_audit = await cls._sift_compaction(
                router, count_data, messages, compact_limit,
            )
            sift_assisted = sift_audit.get("compacted") is True
            attempts.append({
                "model": "sift", "status": "compacted" if sift_assisted else "not_needed",
                "estimated_context_tokens": sift_audit.get("forwarded_tokens"),
                "input_tokens": sift_audit.get("input_tokens"),
                "usage_source": "estimated",
            })
        except Exception as exc:
            attempts.append({
                "model": "sift", "status": "failed",
                "reason": _safe_error_reason(exc),
            })

        if not sift_assisted:
            raise AdvisorContextCompactionError(
                input_tokens=input_tokens, limit=limit, target=compact_limit, attempts=attempts,
            )
        sift_tokens = await cls._count_context_tokens(router, count_data, compaction_source)
        if sift_tokens > compact_limit:
            attempts[0]["status"] = "over_budget"
            attempts[0]["estimated_context_tokens"] = sift_tokens
            raise AdvisorContextCompactionError(
                input_tokens=input_tokens, limit=limit, target=compact_limit, attempts=attempts,
            )

        latest_user = cls._latest_user_index(messages)
        if latest_user < 0 or not any(
            message.get("role") not in {"system", "developer"}
            for message in messages[:latest_user]
        ):
            raise AdvisorContextCompactionError(
                input_tokens=input_tokens, limit=limit, target=compact_limit,
                attempts=[*attempts, {
                    "model": "none", "status": "over_budget",
                    "reason": "no_compactable_prior_turns",
                }],
            )

        if cls._contains_media(messages):
            if any(
                message.get("role") != "user" and cls._contains_media(message.get("content"))
                for message in messages
            ) or any(cls._contains_non_image_media(message.get("content")) for message in messages):
                raise AdvisorContextCompactionError(
                    input_tokens=input_tokens, limit=limit, target=compact_limit,
                    attempts=[*attempts, {
                        "model": "mimo-v2.5-pro", "status": "skipped",
                        "reason": "unsupported_multimodal_input",
                    }],
                )
            try:
                summary, usage = await cls._mimo_compaction(
                    router, compaction_source, multimodal=True,
                )
                compacted_messages = cls._summary_messages(messages, summary)
                compacted_tokens = await cls._count_context_tokens(router, count_data, compacted_messages)
                attempts.append({
                    "model": "mimo-v2.5-pro",
                    "status": "succeeded" if compacted_tokens <= compact_limit else "over_budget",
                    **cls._usage_receipt(usage), "estimated_context_tokens": compacted_tokens,
                })
                if compacted_tokens <= compact_limit:
                    return compacted_messages, {
                        "status": "compacted", "provider": "mimo-v2.5-pro",
                        "assisted_by": ["sift"] if sift_assisted else [],
                        "original_estimated_tokens": input_tokens,
                        "final_estimated_tokens": compacted_tokens,
                        "target_tokens": compact_limit, "attempts": attempts,
                    }
            except Exception as exc:
                attempts.append({
                    "model": "mimo-v2.5-pro", "status": "failed",
                    "reason": _safe_error_reason(exc),
                })
            raise AdvisorContextCompactionError(
                input_tokens=input_tokens, limit=limit, target=compact_limit, attempts=attempts,
            )

        try:
            summary, usage = await cls._minimax_compaction(router, compaction_source)
            compacted_messages = cls._summary_messages(messages, summary)
            compacted_tokens = await cls._count_context_tokens(router, count_data, compacted_messages)
            attempts.append({
                "model": "minimax", "status": "succeeded" if compacted_tokens <= compact_limit else "over_budget",
                **cls._usage_receipt(usage), "estimated_context_tokens": compacted_tokens,
            })
            if compacted_tokens <= compact_limit:
                return compacted_messages, {
                    "status": "compacted", "provider": "minimax",
                    "assisted_by": ["sift"] if sift_assisted else [],
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
            summary, usage = await cls._mimo_compaction(router, compaction_source)
            compacted_messages = cls._summary_messages(messages, summary)
            compacted_tokens = await cls._count_context_tokens(router, count_data, compacted_messages)
            attempts.append({
                "model": "mimo-v2.6-flash",
                "status": "succeeded" if compacted_tokens <= compact_limit else "over_budget",
                **cls._usage_receipt(usage), "estimated_context_tokens": compacted_tokens,
            })
            if compacted_tokens <= compact_limit:
                return compacted_messages, {
                    "status": "compacted", "provider": "mimo-v2.6-flash",
                    "assisted_by": ["sift"] if sift_assisted else [],
                    "original_estimated_tokens": input_tokens,
                    "final_estimated_tokens": compacted_tokens,
                    "target_tokens": compact_limit, "attempts": attempts,
                }
        except Exception as exc:
            attempts.append({
                "model": "mimo-v2.6-flash", "status": "failed",
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
        """Apply the selected advisor policy to supported requests; an empty selection disables it."""

        _, metadata = get_or_create_metadata_bucket(data)
        if call_type not in ADVISOR_CALL_TYPES:
            return None
        tools = data.get("tools")
        if isinstance(tools, list) and any(isinstance(tool, dict) and tool.get("type") == ADVISOR_TOOL_TYPE for tool in tools):
            raise HTTPException(status_code=400, detail="Native advisor tools are retired; use the saved advisor frequency")
        if data.get("gateway_image_request") is not None:
            # The image intent exception owns this request; do not add a
            # saved advisor call, instructions, or provider spending.
            return None
        # The routing callback captures the authoritative saved policy.
        policy = metadata.get("gateway_policy")
        if not isinstance(policy, dict):
            return None
        advisor_model = policy.get("advisor_model")
        if not advisor_model:
            return None
        advisor_effort = policy.get("advisor_reasoning_effort")
        sensitivity = policy.get("advisor_sensitivity", "balanced")
        if sensitivity not in {"conservative", "balanced", "frequent"}:
            raise ValueError("Invalid advisor frequency policy")

        try:
            messages = self._request_messages(data, call_type)
        except ValueError as exc:
            metadata["gateway_advisor"] = {
                "status": "skipped", "reason": "unsupported_request_context",
                "model": advisor_model, "call_type": call_type,
            }
            logger.info(
                "advisor decision request_id=%s call_type=%s target=%s advisor=%s status=skipped reason=unsupported_request_context error_type=%s",
                data.get("litellm_call_id"), call_type, data.get("model"),
                advisor_model, type(exc).__name__,
            )
            return data
        if has_tool_history(messages) and (
            advisor_model not in TOOL_HISTORY_ADVISORS
            or not supports_tool_history(messages)
        ):
            metadata["gateway_advisor"] = {
                "status": "skipped",
                "reason": "unsupported_tool_history",
                "model": advisor_model,
                "call_type": call_type,
            }
            logger.info(
                "advisor decision request_id=%s call_type=%s target=%s advisor=%s status=skipped reason=unsupported_tool_history",
                data.get("litellm_call_id"), call_type, data.get("model"), advisor_model,
            )
            return data

        if (advisor_model in ANTIGRAVITY_ADVISOR_MODELS
                and any(not isinstance(message.get("content", ""), str) for message in messages)):
            metadata["gateway_advisor"] = {"status": "skipped", "reason": "unsupported_content",
                                           "model": advisor_model, "call_type": call_type}
            return data

        request_id = data.get("litellm_call_id") or metadata.setdefault("gateway_advisor_request_id", str(uuid.uuid4()))
        if request_id in self._ranked_attempts:
            raise HTTPException(status_code=409, detail="Duplicate advisor hook; consultation was not repeated")
        self._ranked_attempts[request_id] = None
        if len(self._ranked_attempts) > 1024:
            self._ranked_attempts.popitem(last=False)
        await self._record_analytics(metadata, eligible=True)
        try:
            decision = await self._ranked_decision(messages, request_id=request_id, policy=policy)
        except Exception:
            await self._record_analytics(metadata, failed=True)
            raise
        metadata["gateway_advisor_decision"] = decision
        logger.warning(
            "advisor ranked_selection request_id=%s policy_version=%s score=%.5f target=%.2f consult=%s selected=%s eligible=%s",
            request_id, policy.get("policy_version"), decision["score"], decision["target_rate"],
            decision["consult"], decision["selected_count"], decision["eligible_count"],
        )
        if not decision["consult"]:
            metadata["gateway_advisor"] = {"status": "skipped", "reason": "not_selected_by_ranking",
                                           "model": advisor_model, "call_type": call_type}
            return data

        await self._record_analytics(metadata, selected=True)

        # Advisors receive an independent, bounded copy. Do not gate this copy
        # on the un-compacted target request's limit; target Sift and LiteLLM's
        # provider/fallback path own target admission after this callback.
        from litellm.proxy import proxy_server

        router = proxy_server.llm_router
        consultation_id = uuid.uuid4().hex
        advisor_messages = messages
        resolved_advisor_model = advisor_model
        fallback_attempt: dict[str, str] | None = None
        try:
            if router is None:
                raise RuntimeError("Advisor context limit is unavailable")
            advisor_messages, compaction = await self._prepare_advisor_messages(
                router, advisor_model, data, self._brief_advisor_messages(messages),
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
                await self._check_advisor_dispatch(router, advisor_model, data, advisor_messages)
                await self._record_analytics(metadata, invoked=True, provider_attempts=1)
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
                await self._record_analytics(metadata, invoked=True, provider_attempts=2)
                await self._check_advisor_dispatch(router, fallback_model, data, advisor_messages)
                advice, usage = await self._invoke_advisor(
                    fallback_model, advisor_messages, advisor_effort,
                )
                resolved_advisor_model = fallback_model
        except Exception as exc:
            await self._record_analytics(metadata, failed=True)
            reason = _safe_error_reason(exc)
            gateway_request_id = data.get("litellm_call_id")
            metadata["gateway_advisor"] = {
                "status": "failed", "model": advisor_model,
                "call_type": call_type, "consultation_id": consultation_id, "reason": reason,
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
                metadata["gateway_advisor"] = {
                    "status": "skipped", "model": advisor_model,
                    "call_type": call_type, "reason": "advisor_context_compaction_failed",
                    "gateway_request_id": gateway_request_id,
                }
                # Never send an oversized advisor copy. Advice is supplemental,
                # so a failed compact skips it and leaves target admission to
                # Sift plus LiteLLM's provider/fallback chain.
                return data
            logger.error(
                "advisor consultation failed gateway_request_id=%s consultation_id=%s call_type=%s model=%s base_model=%s error_type=%s error=%s",
                gateway_request_id, consultation_id, call_type, advisor_model,
                data.get("model"), type(exc).__name__, reason,
            )
            if isinstance(exc, AntigravityRequestTooLargeError):
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
            raise HTTPException(
                status_code=status_code,
                detail={
                    "error": {
                        "type": "advisor_consultation_failed",
                        "message": "Selected advisor failed; base request was not sent",
                        "request_id": gateway_request_id,
                        "consultation_id": consultation_id,
                    }
                },
            ) from exc

        # Preserve the original provider receipt even if the independent call
        # below fails and blocks dispatch of the target request.
        metadata["gateway_advisor"] = {
            "status": "advice_ready", "model": advisor_model,
            "resolved_model": resolved_advisor_model, "call_type": call_type,
            "consultation_id": consultation_id, "usage_source": "provider",
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "reasoning_effort": advisor_effort,
            **({"fallback_attempt": fallback_attempt} if fallback_attempt else {}),
        }
        advisor_provider_attempts = 1 + int(fallback_attempt is not None)

        # The selected saved Advisor consultation always receives one additional
        # independent review. Use a distinct copy and the same bounded compaction
        # flow, with the supplemental deployment's strict 32K input cap.
        supplemental_id = uuid.uuid4().hex
        metadata["gateway_advisor_supplemental"] = {
            "status": "pending", "model": SUPPLEMENTAL_ADVISOR_PROVIDER_MODEL,
            "provider": "antigravity", "consultation_id": consultation_id,
            "supplemental_call_id": supplemental_id,
            "gateway_request_id": data.get("litellm_call_id"),
        }
        supplemental_attempted = False
        try:
            from subroute.usage_accounting import direct_attempt, observe_direct
            supplemental_count_data = {**data, "model": SUPPLEMENTAL_ADVISOR_MODEL}
            supplemental_messages, supplemental_compaction = await self._prepare_advisor_messages(
                router, SUPPLEMENTAL_ADVISOR_MODEL, supplemental_count_data,
                self._brief_advisor_messages(messages),
            )
            dispatched_estimated_tokens = (
                supplemental_compaction["final_estimated_tokens"]
                if supplemental_compaction is not None
                else await self._count_context_tokens(
                    router, supplemental_count_data, supplemental_messages,
                )
            )
            metadata["gateway_advisor_supplemental"]["estimated_input_tokens"] = dispatched_estimated_tokens
            dispatched_estimated_tokens = await self._check_advisor_dispatch(
                router, SUPPLEMENTAL_ADVISOR_MODEL, supplemental_count_data, supplemental_messages,
            )
            metadata["gateway_advisor_supplemental"]["estimated_input_tokens"] = dispatched_estimated_tokens
            if supplemental_compaction is not None:
                metadata["gateway_advisor_supplemental"]["original_estimated_input_tokens"] = (
                    supplemental_compaction["original_estimated_tokens"]
                )
                metadata["gateway_advisor_supplemental"]["compaction"] = supplemental_compaction
            supplemental_attempted = True
            await self._record_analytics(
                metadata, invoked=True,
                provider_attempts=advisor_provider_attempts + 1,
            )
            async with direct_attempt(SUPPLEMENTAL_ADVISOR_PROVIDER_MODEL, "antigravity", "advisor"):
                response = await router.acompletion(
                    model=SUPPLEMENTAL_ADVISOR_MODEL,
                    messages=supplemental_messages,
                    num_retries=0,
                    disable_fallbacks=True,
                    metadata={
                        "gateway_advisor_supplemental": True,
                        "gateway_request_id": data.get("litellm_call_id"),
                        "gateway_advisor_consultation_id": consultation_id,
                        "gateway_advisor_supplemental_call_id": supplemental_id,
                    },
                )
                await observe_direct(response)
            supplemental_usage = getattr(response, "usage", None)
            supplemental_advice = self._completion_text(response)
            input_tokens = getattr(supplemental_usage, "prompt_tokens", None)
            output_tokens = getattr(supplemental_usage, "completion_tokens", None)
            if type(input_tokens) is not int or input_tokens < 0 or type(output_tokens) is not int or output_tokens < 0:
                raise RuntimeError("Supplemental Advisor response omitted provider usage")
            metadata["gateway_advisor_supplemental"].update({
                "status": "succeeded", "usage_source": "provider",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            })
        except Exception as exc:
            await self._record_analytics(
                metadata, failed=True,
                provider_attempts=advisor_provider_attempts + int(supplemental_attempted),
            )
            reason = _safe_error_reason(exc)
            metadata["gateway_advisor_supplemental"].update({
                "status": "failed", "usage_source": "unavailable",
                "reason": reason,
            })
            logger.error(
                "supplemental advisor failed gateway_request_id=%s consultation_id=%s supplemental_call_id=%s model=%s provider=antigravity error_type=%s error=%s",
                data.get("litellm_call_id"), consultation_id, supplemental_id,
                SUPPLEMENTAL_ADVISOR_PROVIDER_MODEL, type(exc).__name__, reason,
            )
            raise HTTPException(
                status_code=504 if isinstance(exc, TimeoutError) else 502,
                detail={"error": {
                    "type": "supplemental_advisor_failed",
                    "message": "Supplemental Advisor failed; base request was not sent",
                    "request_id": data.get("litellm_call_id"),
                    "consultation_id": consultation_id,
                    "supplemental_call_id": supplemental_id,
                }},
            ) from exc
        await self._record_analytics(
            metadata, successful=True,
            provider_attempts=advisor_provider_attempts + 1,
        )
        if call_type in MESSAGE_CALL_TYPES:
            data["messages"] = [
                *(data.get("messages") or []),
                {"role": "developer", "content": f"Independent advisor guidance:\n{advice}"},
                {
                    "role": "developer",
                    "content": (
                        "Supplemental independent review (Claude Opus 5.5 Low):\n"
                        f"{supplemental_advice}"
                    ),
                },
            ]
        if call_type in RESPONSES_CALL_TYPES:
            existing_instructions = data.get("instructions")
            guidance = f"Independent advisor guidance:\n{advice}"
            data["instructions"] = (
                f"{existing_instructions}\n\n{guidance}\n\n"
                f"Supplemental independent review (Claude Opus 5.5 Low):\n{supplemental_advice}"
                if isinstance(existing_instructions, str) and existing_instructions
                else (
                    f"{guidance}\n\n"
                    f"Supplemental independent review (Claude Opus 5.5 Low):\n{supplemental_advice}"
                )
            )
        metadata["gateway_advisor"] = {
            "status": "advice_injected", "model": advisor_model,
            "resolved_model": resolved_advisor_model,
            "call_type": call_type,
            "consultation_id": consultation_id, "usage_source": "provider",
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "reasoning_effort": advisor_effort,
            **({"fallback_attempt": fallback_attempt} if fallback_attempt else {}),
        }
        if metadata.get("gateway_advisor_compaction"):
            metadata["gateway_advisor"]["compaction"] = metadata["gateway_advisor_compaction"]
        metadata["gateway_advisor_supplemental"]["advice"] = supplemental_advice
        metadata["gateway_advisor"]["status"] = "advice_injected"
        logger.warning(
            "advisor advice_injected consultation_id=%s call_type=%s model=%s input_tokens=%s output_tokens=%s base_model=%s reasoning_effort=%s",
            consultation_id, call_type, advisor_model, usage.prompt_tokens,
            usage.completion_tokens, data["model"], advisor_effort,
        )
        return data


advisor_plugin_instance = AdvisorPlugin()
