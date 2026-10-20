"""Validate and relay an upstream SSE response."""

from collections.abc import Iterable, Iterator

from sse_parser import ProtocolError, parse_sse


class UpstreamError(RuntimeError):
    pass


def relay_events(upstream: Iterable[bytes]) -> Iterator[dict]:
    """Yield decoded events and one successful terminal, or raise. See README.md."""
    raise NotImplementedError
