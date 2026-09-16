from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import litellm
from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest
from .openai_compatible import LiteLLMClient


@dataclass(frozen=True)
class OpenAISubscriptionAdapter:
    """Call the Codex subscription endpoint through LiteLLM's response bridge."""

    access_token: str
    account_id: str
    api_base: str = "https://chatgpt.com/backend-api/codex"
    client: LiteLLMClient = litellm

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        kwargs = request.completion_kwargs()
        kwargs.pop("max_tokens", None)
        kwargs.update(
            model=f"openai/responses/{request.model}",
            api_base=self.api_base.rstrip("/"),
            api_key=self.access_token,
            extra_headers={
                "ChatGPT-Account-ID": self.account_id,
                "User-Agent": "OpenAI-Codex/0.151.0 (Windows)",
            },
            store=False,
            stream=True,
        )
        result = await self.client.acompletion(**kwargs)
        if isinstance(result, ModelResponse):
            return result
        chunks = [chunk async for chunk in result]
        response = litellm.stream_chunk_builder(chunks, messages=request.messages)
        if not isinstance(response, ModelResponse):
            raise RuntimeError("OpenAI subscription stream did not produce a ModelResponse")
        return response


def openai_subscription_adapter(
    *,
    api_base: str | None = None,
    access_token: str | None = None,
    account_id: str | None = None,
    auth_file: str | Path | None = None,
    client: LiteLLMClient = litellm,
) -> OpenAISubscriptionAdapter:
    if not access_token or not account_id:
        path = Path(
            auth_file
            or os.getenv("CODEX_AUTH_FILE", Path.home() / ".codex" / "auth.json")
        )
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        tokens = data.get("tokens", {})
        access_token = access_token or tokens.get("access_token")
        account_id = account_id or tokens.get("account_id")
    if not access_token or not account_id:
        raise ValueError("OpenAI subscription credentials are unavailable")
    return OpenAISubscriptionAdapter(
        access_token,
        account_id,
        api_base
        or os.getenv(
            "OPENAI_SUBSCRIPTION_BASE_URL", "https://chatgpt.com/backend-api/codex"
        ),
        client,
    )
