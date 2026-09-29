"""LiteLLM provider using the authenticated Antigravity Compose bridge."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jsonschema
from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage
from referencing import Registry
from referencing.exceptions import Unresolvable


MODELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-med": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.7-flash": "Gemini 3.7 Flash (High)",
    "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
    "gemini-3.7-flash-medium": "Gemini 3.7 Flash (Medium)",
    "gemini-3.7-flash-low": "Gemini 3.7 Flash (Low)",
    "gemini-3.6-flash": "Gemini 3.6 Flash (High)",
    "gemini-3.6-flash-high": "Gemini 3.6 Flash (High)",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
}
# Keep in sync with sidecars/antigravity/bridge.py. Reject oversized requests
# before writing a body the sidecar will refuse and reset the HTTP connection.
BRIDGE_MAX_REQUEST_BYTES = 1_000_000


class _ToolOutputError(Exception):
    """The provider completed, but its structured tool decision was unusable."""


class AntigravityBridgeError(RuntimeError):
    """A non-success response from the private Compose CLI bridge."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


class AntigravityRequestTooLargeError(ValueError):
    """The serialized request exceeds the bridge's documented body limit."""


def model_with_effort(model: str, effort: str | None) -> str:
    if effort is None:
        return model
    # The subscription catalog encodes thinking levels in the model variant.
    base = model
    for suffix in ("-high", "-medium", "-med", "-low"):
        if base.endswith(suffix):
            base = base.removesuffix(suffix)
            break
    selected = f"{base}-{effort}"
    if selected not in MODELS:
        raise ValueError(f"Unsupported Antigravity reasoning effort {effort!r} for {model}")
    return selected


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("Antigravity subscription accepts text content blocks only")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Antigravity subscription received an unsupported content block")
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block_type == "tool_use":
            parts.append(
                f"[Tool call {block.get('name', '')} id={block.get('id', '')}]: "
                f"{json.dumps(block.get('input', {}), ensure_ascii=False)}"
            )
        elif block_type == "tool_result":
            result = _content_text(block.get("content", ""))
            status = " error" if block.get("is_error") else ""
            parts.append(f"[Tool result{status} id={block.get('tool_use_id', '')}]: {result}")
        else:
            raise ValueError(f"Antigravity subscription does not support {block_type!r} content blocks")
    return "\n".join(parts)


def prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        raw_content = message.get("content", "")
        if raw_content is None and role == "assistant" and message.get("tool_calls"):
            raw_content = ""
        content = _content_text(raw_content)
        if role == "tool":
            role = f"Tool result {message.get('name') or message.get('tool_call_id') or ''}".strip()
        parts.append(f"[{role.capitalize()}]:\n{content}")
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            parts.append(
                f"[Tool call {function.get('name', '')} id={call.get('id', '')}]: "
                f"{json.dumps(arguments, ensure_ascii=False)}"
            )
    prompt = "\n\n".join(parts)
    if not prompt.strip():
        raise ValueError("Antigravity prompt is empty")
    return prompt


async def invoke_agy(
    model: str,
    prompt: str,
    timeout: float = 120.0,
    *,
    json_schema: dict[str, Any] | None = None,
    advisor: bool = False,
) -> tuple[str, Usage]:
    target = MODELS.get(model)
    if target is None:
        raise ValueError(f"Unsupported Antigravity model: {model}")
    bridge_url = os.getenv("ANTIGRAVITY_BRIDGE_URL")
    if bridge_url:
        payload = {"model": target, "prompt": prompt, "json_schema": json_schema}
        if advisor:
            payload["advisor"] = True
        payload_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if payload_size > BRIDGE_MAX_REQUEST_BYTES:
            raise AntigravityRequestTooLargeError(
                f"Antigravity bridge request exceeds {BRIDGE_MAX_REQUEST_BYTES} bytes"
            )
        bridge_request_id = uuid.uuid4().hex
        try:
            async with httpx.AsyncClient(timeout=timeout + 5) as client:
                response = await client.post(
                    f"{bridge_url.rstrip('/')}/v1/completions",
                    json=payload,
                    headers={"X-Subroute-Request-ID": bridge_request_id},
                )
            if response.status_code >= 400:
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = None
                detail_value = (
                    error_payload.get("detail", response.text)
                    if isinstance(error_payload, dict) else response.text
                )
                detail = detail_value if isinstance(detail_value, str) else str(detail_value)
                detail = detail[:300]
                if not detail:
                    detail = "bridge returned an empty error response"
                raise AntigravityBridgeError(
                    response.status_code,
                    f"Antigravity bridge request_id={bridge_request_id} returned {response.status_code}: {detail}",
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError("Antigravity bridge returned invalid JSON") from exc
            if not isinstance(result, dict):
                raise RuntimeError("Antigravity bridge returned a non-object response")
            content = result.get("content")
            if not isinstance(content, str) or not content:
                raise RuntimeError("Antigravity bridge returned no response text")
            raw_usage = result.get("usage")
            keys = ("input_tokens", "output_tokens", "total_tokens")
            if not isinstance(raw_usage, dict) or not all(
                type(raw_usage.get(key)) is int and raw_usage[key] >= 0 for key in keys
            ):
                raise RuntimeError("Antigravity bridge returned no valid provider usage")
            return content, Usage(
                prompt_tokens=raw_usage["input_tokens"],
                completion_tokens=raw_usage["output_tokens"],
                total_tokens=raw_usage["total_tokens"],
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Antigravity bridge request_id={bridge_request_id} timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Antigravity bridge request_id={bridge_request_id} is unavailable: {type(exc).__name__}"
            ) from exc
    raise RuntimeError(
        "Antigravity subscription requires the authenticated Compose CLI bridge"
    )


class AntigravityLLM(CustomLLM):
    """Subscription-backed text and schema-constrained tool calls via LiteLLM."""

    @staticmethod
    def _request_parts(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, list, dict]:
        model = str(kwargs.get("model") or (args[0] if args else "gemini-3.8-flash"))
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
        optional_params = kwargs.get("optional_params") or {}
        return model, messages, optional_params

    @staticmethod
    def _tools(optional_params: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        tools = optional_params.get("tools") or []
        choice = optional_params.get("tool_choice", "auto")
        if choice == "none" or (isinstance(choice, dict) and choice.get("type") == "none"):
            return [], None
        normalized: list[dict[str, Any]] = []
        for raw_tool in tools:
            if not isinstance(raw_tool, dict):
                dump = getattr(raw_tool, "model_dump", None)
                raw_tool = dump(exclude_none=True) if callable(dump) else None
            if not isinstance(raw_tool, dict):
                raise ValueError("Antigravity tool definition must be an object")
            function = raw_tool.get("function", raw_tool)
            if not isinstance(function, dict):
                raise ValueError("Antigravity tool function must be an object")
            name = function.get("name")
            parameters = function.get("parameters", function.get("input_schema"))
            if not isinstance(name, str) or not name or not isinstance(parameters, dict):
                raise ValueError("Antigravity tools require a name and JSON Schema parameters")
            if parameters.get("type", "object") != "object":
                raise ValueError(f"Antigravity tool {name!r} parameters must be an object schema")
            try:
                jsonschema.Draft202012Validator.check_schema(parameters)
            except jsonschema.SchemaError as exc:
                raise ValueError(f"Antigravity tool {name!r} has an invalid JSON Schema: {exc.message}") from exc
            # Tool schemas must be self-contained. jsonschema's default resolver
            # can otherwise fetch a caller-controlled URL synchronously.
            pending = [parameters]
            while pending:
                node = pending.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(value, str) and not value.startswith("#"):
                            raise ValueError("Antigravity tool schemas support only local fragment references")
                        if isinstance(value, (dict, list)):
                            pending.append(value)
                elif isinstance(node, list):
                    pending.extend(item for item in node if isinstance(item, (dict, list)))
            normalized.append({
                "name": name,
                "description": str(function.get("description", "")),
                "parameters": parameters,
            })

        if not normalized:
            return [], None
        selected: str | None = None
        required = False
        if isinstance(choice, str):
            required = choice in {"required", "any"}
        elif isinstance(choice, dict):
            choice_type = choice.get("type")
            required = choice_type in {"any", "tool"}
            if choice_type == "function":
                selected = (choice.get("function") or {}).get("name")
                required = True
            elif choice_type == "tool":
                selected = choice.get("name")
                required = True
        if selected is not None:
            normalized = [tool for tool in normalized if tool["name"] == selected]
            if not normalized:
                raise ValueError(f"tool_choice selected undeclared tool {selected!r}")
        return normalized, "tool_call" if required else None

    @staticmethod
    def _tool_schema(tools: list[dict[str, Any]], forced_kind: str | None) -> dict[str, Any]:
        kinds = [forced_kind] if forced_kind else ["text", "tool_call"]
        required = ["kind", "name", "input"] if forced_kind == "tool_call" else ["kind"]
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": kinds},
                "text": {"type": "string"},
                "name": {"type": "string", "enum": [tool["name"] for tool in tools]},
                "input": {"type": "object"},
            },
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _tool_prompt(prompt: str, tools: list[dict[str, Any]], forced_kind: str | None) -> str:
        tool_data = [
            {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
            for tool in tools
        ]
        policy = (
            "You must choose exactly one listed tool for this turn."
            if forced_kind == "tool_call" else
            "Choose a listed tool only when it is needed; otherwise return a text answer."
        )
        return (
            f"{prompt}\n\nAvailable client tools (the client executes them):\n"
            f"{json.dumps(tool_data, ensure_ascii=False)}\n\n{policy} "
            "Never execute or simulate a tool. Return only the schema-constrained JSON decision. "
            "For a tool call, set kind to tool_call and include its exact name and input object. "
            "For a normal answer, set kind to text and include text."
        )

    @classmethod
    def _parse_tool_decision(cls, content: str, *, allow_plain_text: bool = False) -> dict[str, Any]:
        candidate = re.sub(r"(?m)^\s*```(?:json)?\s*$", "", content).lstrip()
        decoder = json.JSONDecoder()
        try:
            decision, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError as exc:
            # Only an unstructured answer may use the auto-choice text path.
            # Never turn malformed JSON or a rejected structured result into success.
            if allow_plain_text and not content.lstrip().startswith(("{", "[", "```")):
                return {"kind": "text", "text": content}
            raise _ToolOutputError(f"Antigravity returned invalid tool JSON: {exc.msg}") from exc
        if not isinstance(decision, dict):
            raise _ToolOutputError("Antigravity returned a non-object tool decision")
        remainder = candidate[end:].lstrip()
        while remainder.startswith("{"):
            try:
                extra, extra_end = decoder.raw_decode(remainder)
            except json.JSONDecodeError as exc:
                raise _ToolOutputError(f"Antigravity returned invalid trailing JSON: {exc.msg}") from exc
            is_finish_marker = (
                isinstance(extra, dict)
                and extra.get("kind") == "tool_call"
                and extra.get("name") == "finish"
                and extra.get("input") == {}
                and set(extra) <= {"kind", "name", "input", "id", "type"}
            )
            if extra != decision and not is_finish_marker:
                extra_name = extra.get("name") if isinstance(extra, dict) else None
                raise _ToolOutputError(
                    f"Antigravity returned a different additional structured result (name={extra_name!r})"
                )
            remainder = remainder[extra_end:].lstrip()
        trailing = remainder.strip()
        decision_text = decision.get("text")
        if trailing and isinstance(decision_text, str) and cls._is_repeated_text_tail(trailing, decision_text):
            trailing = ""
        if trailing:
            # AGY may append natural-language completion commentary after its
            # schema-constrained result. It is not part of the gateway's tool
            # contract, so ignore bounded plain text, while still rejecting
            # any additional structured result above.
            if len(trailing) > 1200:
                raise _ToolOutputError("Antigravity returned an oversized postamble after its structured tool decision")
            offset = 0
            while True:
                object_start = trailing.find("{", offset)
                if object_start < 0:
                    break
                try:
                    extra, extra_end = decoder.raw_decode(trailing, object_start)
                except json.JSONDecodeError:
                    offset = object_start + 1
                    continue
                is_finish_marker = (
                    isinstance(extra, dict)
                    and extra.get("kind") == "tool_call"
                    and extra.get("name") == "finish"
                    and extra.get("input") == {}
                    and set(extra) <= {"kind", "name", "input", "id", "type"}
                )
                if extra != decision and not is_finish_marker:
                    extra_name = extra.get("name") if isinstance(extra, dict) else None
                    extra_shape = (
                        f"type={extra.get('type')!r}, kind={extra.get('kind')!r}, "
                        f"fields={sorted(extra.keys())!r}"
                        if isinstance(extra, dict) else f"type={type(extra).__name__}"
                    )
                    raise _ToolOutputError(
                        "Antigravity returned a different additional structured result "
                        f"(name={extra_name!r}, {extra_shape})"
                    )
                offset = extra_end
        return decision

    @staticmethod
    def _is_repeated_text_tail(tail: str, expected: str) -> bool:
        if not expected:
            return False
        remaining = tail.strip()
        if remaining != expected and not remaining.startswith(expected + "\n"):
            return False
        while remaining.startswith(expected):
            remaining = remaining[len(expected):].strip()
        return not remaining

    @staticmethod
    async def _complete(
        model: str,
        messages: list,
        optional_params: dict,
    ) -> tuple[str | None, Usage, list[dict[str, Any]]]:
        # Owner-approved compatibility boundary: AGY has no output-token-cap
        # control. Keep its completed output and provider usage intact; README
        # and the control desk disclose backend-managed subscription limits.
        try:
            tools, forced_kind = AntigravityLLM._tools(optional_params)
            model = model_with_effort(model, optional_params.get("reasoning_effort"))
            prompt = prompt_from_messages(messages)
            schema = AntigravityLLM._tool_schema(tools, forced_kind) if tools else None
            if tools:
                prompt = AntigravityLLM._tool_prompt(prompt, tools, forced_kind)
            content, usage = await invoke_agy(
                model,
                prompt,
                json_schema=schema,
            )
            if not tools:
                return content, usage, []
            decision = AntigravityLLM._parse_tool_decision(
                content, allow_plain_text=forced_kind is None,
            )
            if decision.get("kind") not in {"text", "tool_call"}:
                raise _ToolOutputError("Antigravity returned an invalid tool decision")
            if decision["kind"] == "text":
                if set(decision) - {"kind", "text"}:
                    raise _ToolOutputError("Antigravity returned extra fields in a text decision")
                if forced_kind == "tool_call" or not isinstance(decision.get("text"), str):
                    raise _ToolOutputError("Antigravity returned text when a tool call was required")
                return decision["text"], usage, []
            name, arguments = decision.get("name"), decision.get("input")
            if set(decision) != {"kind", "name", "input"}:
                raise _ToolOutputError("Antigravity returned missing or extra fields in a tool decision")
            tool = next((item for item in tools if item["name"] == name), None)
            if tool is None or not isinstance(arguments, dict):
                raise _ToolOutputError("Antigravity returned an undeclared tool or invalid tool input")
            jsonschema.validate(arguments, tool["parameters"], registry=Registry())
            return None, usage, [{
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }]
        except AntigravityBridgeError as exc:
            raise CustomLLMError(status_code=exc.status_code, message=str(exc)) from exc
        except (_ToolOutputError, jsonschema.ValidationError) as exc:
            raise CustomLLMError(status_code=502, message=str(exc)) from exc
        except Unresolvable as exc:
            raise CustomLLMError(status_code=400, message="Tool schema contains an unresolved local reference") from exc
        except ValueError as exc:
            raise CustomLLMError(status_code=400, message=str(exc)) from exc
        except TimeoutError as exc:
            raise CustomLLMError(status_code=504, message=str(exc)) from exc
        except (RuntimeError, OSError) as exc:
            raise CustomLLMError(status_code=502, message=str(exc)) from exc

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model, messages, optional_params = self._request_parts(args, kwargs)
        if optional_params.get("stream"):
            raise CustomLLMError(status_code=400, message="Use Antigravity's buffered streaming adapter")
        content, usage, tool_calls = await self._complete(model, messages, optional_params)

        return ModelResponse(
            id=f"chatcmpl-agy-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls or None},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage=usage,
        )

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:
        model, messages, optional_params = self._request_parts(args, kwargs)
        content, usage, tool_calls = await self._complete(model, messages, optional_params)
        if content:
            yield {
                "text": content,
                "is_finished": False,
                "finish_reason": "",
                "usage": None,
                "index": 0,
            }
        for index, call in enumerate(tool_calls):
            yield {
                "text": "",
                "tool_use": {
                    "id": call["id"],
                    "type": "function",
                    "function": call["function"],
                    "index": index,
                },
                "is_finished": False,
                "finish_reason": "",
                "usage": None,
                "index": 0,
            }
        yield {
            "text": "",
            "is_finished": True,
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "index": 0,
        }


antigravity_handler = AntigravityLLM()
