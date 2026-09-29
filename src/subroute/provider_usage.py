"""Manual, provider-reported quota reads for presentation clients.

This module is intentionally outside the request-routing path. A read is made
only when a local UI asks to refresh, and unavailable provider APIs remain
unavailable rather than being converted into guessed quota values.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx


_CACHE: dict[str, Any] | None = None
_CACHE_LOCK = threading.Lock()
_REFRESH_LOCK = asyncio.Lock()


def _unavailable(detail: str) -> dict[str, str]:
    return {"state": "unavailable", "detail": detail}


def _ready(used: float, limit: float, detail: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "detail": detail,
    }


async def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: float) -> Any:
    """Make one cancellable provider request with its own bounded timeout."""
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()


async def _codex_subscription_usage() -> dict[str, Any]:
    auth_path = Path(os.getenv("CODEX_AUTH_FILE", Path.home() / ".codex" / "auth.json"))
    try:
        tokens = json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]
        access_token = tokens["access_token"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return _unavailable("Codex subscription credentials are unavailable")

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": "OpenAI-Codex"}
    if account_id := tokens.get("account_id"):
        headers["chatgpt-account-id"] = account_id
    try:
        payload = await _get_json("https://chatgpt.com/backend-api/wham/usage", headers=headers, timeout=6.0)
        window = (payload.get("rate_limit") or {}).get("primary_window") or {}
        used = window.get("used_percent")
        if not isinstance(used, (int, float)):
            return _unavailable("OpenAI subscription response omitted the primary quota window")
        reset = window.get("reset_after_seconds")
        suffix = f"resets in about {int(reset) // 60} min" if isinstance(reset, (int, float)) else "primary quota window"
        return _ready(float(used), 100, suffix)
    except httpx.HTTPError as exc:
        return _unavailable(f"OpenAI subscription usage read failed: {type(exc).__name__}")


async def _minimax_usage() -> dict[str, Any]:
    key = os.getenv("MINIMAX_API_KEY")
    if not key:
        return _unavailable("MiniMax API key is not configured")
    try:
        payload = await _get_json("https://api.minimax.io/v1/token_plan/remains", headers={"Authorization": f"Bearer {key}"}, timeout=6.0)
        plans = payload.get("model_remains") or []
        general = next((plan for plan in plans if plan.get("model_name") == "general"), None)
        remaining = general.get("current_interval_remaining_percent") if isinstance(general, dict) else None
        if not isinstance(remaining, (int, float)):
            return _unavailable("MiniMax response omitted the general token-plan window")
        return _ready(100 - float(remaining), 100, "current token-plan window")
    except httpx.HTTPError as exc:
        return _unavailable(f"MiniMax usage read failed: {type(exc).__name__}")


async def _openrouter_usage() -> dict[str, Any]:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return _unavailable("OpenRouter API key is not configured")
    try:
        payload = await _get_json("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}, timeout=6.0)
        data = payload.get("data") or {}
        total = data.get("total_credits")
        used = data.get("total_usage")
        if not isinstance(total, (int, float)) or not isinstance(used, (int, float)) or total <= 0:
            return _unavailable("OpenRouter response omitted usable credit totals")
        return _ready(float(used), float(total), "USD credits")
    except httpx.HTTPError as exc:
        return _unavailable(f"OpenRouter usage read failed: {type(exc).__name__}")


async def _gemini_subscription_usage() -> dict[str, Any]:
    bridge_url = os.getenv("ANTIGRAVITY_BRIDGE_URL")
    if not bridge_url:
        return _unavailable("Gemini subscription Docker bridge is not configured")
    try:
        status = await _get_json(f"{bridge_url.rstrip('/')}/v1/status", timeout=18.0)
        if status.get("authenticated") is False:
            return {
                "state": "sign_in_required",
                "detail": str(status.get("detail") or "Antigravity sign-in required"),
            }
        if status.get("authenticated") is not True:
            return _unavailable(str(status.get("detail") or "Antigravity status unavailable"))
        models = status.get("models")
        count = len(models) if isinstance(models, list) else 0
        return {
            "state": "connected",
            "detail": f"Authenticated Docker bridge; {count} subscription models available",
        }
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        return _unavailable(f"Gemini subscription bridge read failed: {type(exc).__name__}")


async def _read_source(name: str, reader: Any) -> dict[str, Any]:
    """Keep malformed or unexpected results local to their provider card."""
    try:
        return await reader()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _unavailable(f"{name} usage read failed: {type(exc).__name__}")


async def read_provider_usage(*, refresh: bool = False) -> dict[str, Any]:
    """Return cached data or refresh providers concurrently outside the event loop."""
    global _CACHE
    with _CACHE_LOCK:
        original_snapshot = _CACHE
        if original_snapshot is not None and not refresh:
            return original_snapshot

    # Coalesce simultaneous cache misses/refreshes into one bounded set of
    # provider requests. The cache lock is never held across network I/O.
    async with _REFRESH_LOCK:
        with _CACHE_LOCK:
            if _CACHE is not None and _CACHE is not original_snapshot:
                return _CACHE
            if _CACHE is not None and not refresh:
                return _CACHE

        readers = (
            ("openai-subscription", _codex_subscription_usage),
            ("minimax", _minimax_usage),
            ("openrouter", _openrouter_usage),
            ("gemini-subscription", _gemini_subscription_usage),
        )
        results = await asyncio.gather(*(_read_source(name, reader) for name, reader in readers))
        snapshot = {
            "sources": {name: result for (name, _), result in zip(readers, results, strict=True)},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with _CACHE_LOCK:
            _CACHE = snapshot
        return snapshot
