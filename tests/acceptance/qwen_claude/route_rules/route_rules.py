"""Choose a model after applying the gateway's saved routing policy."""

VIRTUAL_ALIASES = frozenset({"current", "default"})


def resolve_model(requested: str, active: str, mode: str) -> str:
    """Return the model that should receive this request."""
    if mode not in {"off", "alias", "force"}:
        raise ValueError(f"unknown routing mode: {mode}")
    if mode == "off":
        return requested
    if mode == "force" or requested not in VIRTUAL_ALIASES:
        return active
    return requested
