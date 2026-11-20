# Scoped technical debt

- Production reads its saved route and subscription catalog from a legacy bind
  mount. Staging uses a named state volume. Copy the two production files
  together if the bind mount moves.
- The pinned LiteLLM release needs compatibility repairs for some tool schemas,
  Anthropic thinking blocks, and streamed Responses results. Run the
  [client contract suite](tests/test_client_stream_contracts.py) when upgrading.
