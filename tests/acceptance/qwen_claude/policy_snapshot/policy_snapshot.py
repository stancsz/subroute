"""Atomic routing and advisor policy snapshots for a gateway fixture."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading


@dataclass(frozen=True)
class Policy:
    active_model: str
    mode: str
    advisor_model: str | None
    revision: int


@dataclass(frozen=True)
class Decision:
    model: str
    advisor_model: str | None
    revision: int


class PolicyStore:
    """One authoritative policy per environment, persisted as JSON.

    A request must resolve route and advisor using the same immutable policy
    revision, even if an update overlaps it. Updates must be validated before
    publication and written atomically. Production and staging paths must be
    independent. Existing valid state survives a failed update or corrupt
    replacement file. The virtual aliases are `current` and `default`; `auto`
    is always passed through. Off preserves requests, alias mode only maps
    virtual aliases, and force mode maps every explicit model.
    """

    def __init__(self, path: Path, default: Policy):
        raise NotImplementedError

    def snapshot(self) -> Policy:
        raise NotImplementedError

    def update(self, *, active_model: str, mode: str, advisor_model: str | None) -> Policy:
        raise NotImplementedError

    def resolve(self, requested: str) -> Decision:
        raise NotImplementedError
