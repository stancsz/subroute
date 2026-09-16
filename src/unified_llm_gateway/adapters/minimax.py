from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from litellm import Router
from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest
from .openai_compatible import LiteLLMClient


RouterFactory = Callable[..., LiteLLMClient]


@dataclass
class MiniMaxAdapter:
    """Use LiteLLM Router to balance multiple direct MiniMax API keys."""

    api_keys: Sequence[str]
    router_factory: RouterFactory = Router
    _routers: dict[str, LiteLLMClient] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        keys = tuple(dict.fromkeys(key.strip() for key in self.api_keys if key.strip()))
        if not keys:
            raise ValueError("At least one MiniMax API key is required")
        self.api_keys = keys

    def _router(self, model: str) -> LiteLLMClient:
        if model not in self._routers:
            provider_model = model if model.startswith("minimax/") else f"minimax/{model}"
            self._routers[model] = self.router_factory(
                model_list=[
                    {
                        "model_name": model,
                        "litellm_params": {"model": provider_model, "api_key": key},
                    }
                    for key in self.api_keys
                ],
                routing_strategy="simple-shuffle",
                num_retries=0,
                fallbacks=[],
            )
        return self._routers[model]

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        return await self._router(request.model).acompletion(
            **request.completion_kwargs()
        )


def minimax_adapter(
    *,
    api_keys: Sequence[str] | None = None,
    router_factory: RouterFactory = Router,
) -> MiniMaxAdapter:
    if api_keys is None:
        combined = os.getenv("MINIMAX_API_KEYS", "").split(",")
        combined.append(os.getenv("MINIMAX_API_KEY", ""))
        api_keys = combined
    return MiniMaxAdapter(api_keys, router_factory)
