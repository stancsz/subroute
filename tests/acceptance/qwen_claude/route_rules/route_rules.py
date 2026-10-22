"""Choose a model after applying the gateway's saved routing policy."""

VIRTUAL_ALIASES = frozenset({"default"})


def resolve_model(requested: str, active: str, mode: str) -> str:
    """Return the model that should receive this request."""
    if mode not in {"off", "alias", "force"}:
        raise ValueError(f"unknown routing mode: {mode}")
    if requested == "current":
        raise ValueError("unknown model")
    if requested == "auto":
        return requested
    if requested in VIRTUAL_ALIASES:
        return active
    if mode == "off":
        return requested
    if mode == "force":
        return active
    return requested
