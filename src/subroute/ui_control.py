"""Shared browser and Electron control-desk presentation routes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse

from subroute.provider_usage import read_provider_usage


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = ROOT / "config" / "ui_sources.json"
CONTROL_PATH = Path(__file__).resolve().parent / "control"


def source_configuration() -> list[dict[str, object]]:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    sources: list[dict[str, object]] = []
    for item in payload.get("sources", []):
        if not all(isinstance(item.get(key), str) for key in ("id", "name", "kind", "models")):
            continue
        credentials = item.get("credentials", [])
        if not isinstance(credentials, list) or not all(isinstance(value, str) for value in credentials):
            credentials = []
        available = item.get("available", True) is not False
        configured = available and all(os.getenv(key) for key in credentials)
        sources.append({
            "id": item["id"], "name": item["name"], "kind": item["kind"],
            "models": item["models"], "accent": item.get("accent", "blue"),
            "available": available, "configured": configured,
        })
    return sorted(sources, key=lambda source: str(source["name"]).casefold())


async def control_desk(_: Request) -> FileResponse:
    return FileResponse(CONTROL_PATH / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


async def control_styles(_: Request) -> FileResponse:
    return FileResponse(CONTROL_PATH / "styles.css", media_type="text/css", headers={"Cache-Control": "no-cache"})


async def control_script(_: Request) -> FileResponse:
    return FileResponse(CONTROL_PATH / "app.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


async def source_status(_: Request) -> dict[str, object]:
    return {"sources": source_configuration()}


async def provider_usage(refresh: bool = False) -> dict[str, object]:
    return read_provider_usage(refresh=refresh)


def register_ui_routes(app: object) -> None:
    existing = {(getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", None) or []))) for route in app.routes}
    routes = (
        ("/control", control_desk, FileResponse),
        ("/control/styles.css", control_styles, FileResponse),
        ("/control/app.js", control_script, FileResponse),
        ("/api/source-status", source_status, None),
        ("/api/provider-usage", provider_usage, None),
    )
    for path, endpoint, response_class in routes:
        key = (path, ("GET",))
        if key in existing:
            continue
        options: dict[str, object] = {"methods": ["GET"], "include_in_schema": False}
        if response_class is not None:
            options["response_class"] = response_class
        app.add_api_route(path, endpoint, **options)
