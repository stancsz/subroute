"""Incremental Server-Sent Events parser for byte chunks."""

from collections.abc import Iterable, Iterator


class ProtocolError(ValueError):
    pass


def parse_sse(chunks: Iterable[bytes], *, max_event_bytes: int = 65_536) -> Iterator[tuple[str, str]]:
    """Yield complete (event name, data text) pairs. See README.md."""
    raise NotImplementedError
