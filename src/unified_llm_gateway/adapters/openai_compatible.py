from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import litellm
from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest


class LiteLLMClient(Protocol):
    async def acompletion(self, **kwargs: Any) -> Any: ...


def _openai_model(model: str) -> str:
    return model if "/" in model else f"openai/{model}"


@dataclass(frozen=True)
class LiteLLMAdapter:
    """A thin LiteLLM adapter for one OpenAI-compatible endpoint."""

    name: str
    api_base: str
    api_key: str = "local"
    client: LiteLLMClient = litellm
    fixed_options: dict[str, Any] = field(default_factory=dict)

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        kwargs = request.completion_kwargs()
        kwargs.update(
            model=_openai_model(request.model),
            api_base=self.api_base.rstrip("/"),
            api_key=self.api_key,
        )
        kwargs.update(self.fixed_options)
        return await self.client.acompletion(**kwargs)


def freetoken_adapter(
    *, api_base: str | None = None, client: LiteLLMClient = litellm
) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        "freetoken",
        api_base or os.getenv("FREETOKEN_BASE_URL", "http://127.0.0.1:1919/v1"),
        client=client,
        fixed_options={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )


def desktop_adapter(
    *, api_base: str | None = None, client: LiteLLMClient = litellm
) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        "desktop",
        api_base or os.getenv("DESKTOP_LLM_BASE_URL", "http://127.0.0.1:11434/v1"),
        client=client,
    )
