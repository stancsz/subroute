"""Static assets for the loopback model selector."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def register_ui_routes(app: FastAPI) -> None:
    if not any(getattr(route, "path", None) == "/control" for route in app.routes):
        app.mount("/control", StaticFiles(directory=Path(__file__).with_name("control"), html=True))
