# Subroute architecture

Both gateways run the same LiteLLM configuration and application code.
LiteLLM owns the public APIs, provider dispatch, streaming, declared
fallbacks and spend logs. The local selector saves a target and validates configuration
and provider catalog entries. `default` follows that selection.

Codex subscriptions use the account credentials mounted in the gateway.
Antigravity subscriptions use a single optional CLI bridge on the Compose
network. API deployments are declared in LiteLLM's `model_list`.

Production uses `litellm` and staging uses `litellm_staging` on the same
PostgreSQL server. Routing state and gateway networks are
separate. The optional Antigravity bridge shares its subscription and quota.

The stream and tool-schema compatibility patches are tied to the LiteLLM pin.
Upgrade them through the [client contract suite](../../tests/test_client_stream_contracts.py).
