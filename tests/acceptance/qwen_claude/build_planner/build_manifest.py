"""Validate a CI build manifest."""

from dataclasses import dataclass


class BuildError(ValueError):
    """A manifest or build plan cannot be represented safely."""


@dataclass(frozen=True)
class Target:
    name: str
    command: str
    sources: tuple[str, ...]
    deps: tuple[str, ...]


def load_manifest(raw_json: str) -> dict[str, Target]:
    raise NotImplementedError
