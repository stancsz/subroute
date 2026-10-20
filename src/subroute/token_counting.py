"""Counter-only compatibility repair for the pinned LiteLLM releases."""

from importlib.metadata import version


def install_tool_schema_counting_fix() -> None:
    """Count unconstrained arrays without changing the provider's schema.

    LiteLLM 1.101.0/1.103.0 indexes array ``items`` unconditionally. JSON
    Schema permits its omission, meaning any item type. Use the formatter's
    existing ``any`` spelling for that case, including nested properties.
    Retire this shim when the deployed formatter supports omitted items.
    """
    if version("litellm") not in {"1.101.0", "1.103.0"}:
        return
    from litellm.litellm_core_utils import token_counter

    original = token_counter._format_type
    if getattr(original, "_subroute_array_items_fix", False):
        return

    def format_type(props, indent):
        if props.get("type") == "array" and "items" not in props:
            return "any[]"
        return original(props, indent)

    format_type._subroute_array_items_fix = True
    token_counter._format_type = format_type
