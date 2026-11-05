"""On-demand subscription inventory, separate from configured inference routes."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from subroute.plugins.codex_credentials import read_codex_credentials
from subroute.provider_usage import _get_json

_lock = asyncio.Lock()
_snapshot: dict[str, Any] | None = None
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


def catalog_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.stem}.catalog.json")


def catalog_alias(provider: str, model: str) -> str:
    if provider == "openai":
        return f"catalog-openai/{model}"
    if provider == "gemini":
        # Keep Claude names out of LiteLLM's Anthropic name heuristics.
        return f"catalog-antigravity/{model.encode('ascii').hex()}"
    raise ValueError("invalid catalog provider")


def _validated_models(models: Any) -> list[str]:
    if not isinstance(models, list) or len(models) > 128 or any(
        not isinstance(model, str) or not _MODEL_ID.fullmatch(model) for model in models
    ):
        raise ValueError("invalid provider model inventory")
    return sorted(set(models))


class SavedEffortUnavailable(ValueError):
    """A refresh would hide the user's saved reasoning policy."""


class ModelInventory(list[str]):
    def __init__(self, models: list[str], efforts: dict[str, list[str]]) -> None:
        super().__init__(models)
        self.efforts = efforts


def _validated_efforts(value: Any, models: list[str]) -> dict[str, list[str]]:
    allowed = {"low", "medium", "high", "xhigh", "max", "ultra"}
    if not isinstance(value, dict) or any(
        model not in models or not isinstance(levels, list)
        or any(not isinstance(level, str) or level not in allowed for level in levels)
        for model, levels in value.items()
    ):
        raise ValueError("invalid provider reasoning efforts")
    return {model: list(dict.fromkeys(levels)) for model, levels in value.items()}


def read_catalog(state_path: Path) -> dict[str, Any]:
    path = catalog_path(state_path)
    if not path.exists():
        return {"openai": [], "gemini": [], "openai_current": [], "gemini_current": [], "openai_efforts": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = ("openai", "gemini", "openai_current", "gemini_current")
    if not isinstance(data, dict) or not set(keys).issubset(data) or set(data) - set(keys) - {"openai_efforts"}:
        raise ValueError("invalid provider catalog")
    result = {key: _validated_models(data[key]) for key in keys}
    result["openai_efforts"] = _validated_efforts(data.get("openai_efforts", {}), result["openai"])
    if any(not set(result[f"{provider}_current"]).issubset(result[provider]) for provider in ("openai", "gemini")):
        raise ValueError("current models must be registered")
    return result


def write_catalog(state_path: Path, data: dict[str, Any]) -> None:
    path = catalog_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


async def _openai_models() -> list[str]:
    token, account = read_codex_credentials()
    data = await _get_json(
        "https://chatgpt.com/backend-api/codex/models?client_version=0.166.0",
        headers={"Authorization": f"Bearer {token}", "ChatGPT-Account-ID": account,
                 "User-Agent": "OpenAI-Codex/0.166.0"}, timeout=8.0,
    )
    models = data.get("models")
    if not isinstance(models, list):
        raise ValueError("invalid OpenAI inventory")
    visible = [m for m in models if isinstance(m, dict)
               and isinstance(m.get("slug"), str) and m["slug"]
               and m.get("visibility") == "list" and m.get("supported_in_api") is True]
    efforts = {}
    for model in visible:
        levels = model.get("supported_reasoning_levels", [])
        if isinstance(levels, list):
            efforts[model["slug"]] = [item["effort"] for item in levels
                                      if isinstance(item, dict) and isinstance(item.get("effort"), str)
                                      and item["effort"] in {"low", "medium", "high", "xhigh", "max", "ultra"}]
    return ModelInventory(sorted({m["slug"] for m in visible}), efforts)


async def _gemini_models() -> list[str]:
    url = os.getenv("ANTIGRAVITY_BRIDGE_URL")
    if not url:
        raise RuntimeError("Antigravity bridge is not configured")
    data = await _get_json(f"{url.rstrip('/')}/v1/status", timeout=18.0)
    if data.get("authenticated") is not True or not isinstance(data.get("models"), list):
        raise RuntimeError("Antigravity bridge inventory unavailable or sign-in required")
    return sorted({m for m in data["models"] if isinstance(m, str) and m})


async def _read(reader: Any) -> dict[str, Any]:
    try:
        models = await reader()
        result = {"state": "ready", "models": list(models)}
        if isinstance(models, ModelInventory):
            result["reasoning_efforts"] = models.efforts
        return result
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"state": "unavailable", "models": [], "detail": "Could not refresh models. Check the provider connection and try again."}


async def refresh_catalog() -> dict[str, Any]:
    global _snapshot
    previous = _snapshot
    async with _lock:
        if _snapshot is not previous and _snapshot is not None:
            return _snapshot
        openai, gemini = await asyncio.gather(_read(_openai_models), _read(_gemini_models))
        result = {"openai": openai, "gemini": gemini}
        for key, item in result.items():
            if item["state"] == "ready":
                try:
                    from subroute.plugins.dynamic_router import control_plane
                    control_plane.add_catalog_models(key, _validated_models(item["models"]),
                                                     item.get("reasoning_efforts"))
                except SavedEffortUnavailable:
                    item["state"] = "unavailable"
                    item["models"] = []
                    item["detail"] = "The saved reasoning effort is no longer offered. Choose Default or another effort, then refresh again."
                except (OSError, ValueError):
                    item["state"] = "unavailable"
                    item["models"] = []
                    item["detail"] = "Could not save provider models. Existing routes are unchanged."
            if item["state"] == "unavailable" and previous and previous[key]["state"] in {"ready", "stale"} and previous[key]["models"]:
                item["models"] = previous[key]["models"]
                item["state"] = "stale"
            elif item["state"] == "unavailable":
                from subroute.plugins.dynamic_router import control_plane
                if control_plane.catalog[f"{key}_current"]:
                    item["models"] = list(control_plane.catalog[f"{key}_current"])
                    item["state"] = "stale"
        _snapshot = result
        return result
