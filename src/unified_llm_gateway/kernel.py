from __future__ import annotations

from typing import Any, Protocol

from litellm.types.utils import ModelResponse
from pydantic import BaseModel, ConfigDict, Field


class CompletionRequest(BaseModel):
    """The small subset of LiteLLM completion inputs owned by the kernel."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    timeout: float | None = Field(default=None, gt=0)

    def completion_kwargs(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CompletionAdapter(Protocol):
    async def complete(self, request: CompletionRequest) -> ModelResponse: ...


async def complete(
    request: CompletionRequest, adapter: CompletionAdapter
) -> ModelResponse:
    """Return the adapter's standard LiteLLM response without wrapping it."""

    return await adapter.complete(request)
