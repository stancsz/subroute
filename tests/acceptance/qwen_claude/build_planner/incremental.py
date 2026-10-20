"""Compute a deterministic, content-addressed incremental build plan."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    order: tuple[str, ...]
    rebuild: tuple[str, ...]
    fingerprints: dict[str, str]


def plan_build(targets, source_digests, previous_fingerprints) -> Plan:
    raise NotImplementedError
