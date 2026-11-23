# A Local LiteLLM Gateway for Subscription and API Model Routing

Configuration ownership, saved route selection, and environment separation

## Abstract

Subroute provides a local LiteLLM endpoint for coding clients [R3] while maintaining adapters for Codex and Antigravity subscriptions. LiteLLM owns configured API-provider routes, fallbacks, retries, and usage reporting; a local selector presents an explicit saved choice or default route. Production and staging state are kept separate in the intended deployment. This systems note describes current ownership boundaries and operational model. It reports no controlled latency, reliability, cost, or security evaluation, and a working subscription adapter does not guarantee permanent provider support. We provide an evaluation plan for protocol compatibility, state isolation, route correctness, and failure behavior.

## 1. Motivation and intended use

Coding clients support different providers, protocols, and subscription-backed flows. A local gateway can centralize client endpoints without reimplementing standard provider protocols. Subroute keeps subscription adapters separate from ordinary LiteLLM API routes.

The default alias follows the saved selection; an explicit model name uses its configured LiteLLM route.

## 2. Ownership and product values

LiteLLM owns standard API routing, protocol handling, configured retries, fallbacks, and usage reporting [R1]. An OpenAI-compatible client can use Chat Completions [R2]. The local selector owns the saved default choice. The default Compose stack includes gateway and PostgreSQL; the Antigravity bridge is optional.

Product value is predictable local access with clear ownership of routing policy. Keep mutable production and staging state separate; separate ports alone do not prove isolation when configuration, credentials, or databases are shared.

## Product and research values

Subroute gives coding clients one local LiteLLM endpoint. It maintains Codex and Antigravity subscription adapters; LiteLLM owns configured API routes, fallbacks, retries, and usage reporting.

- **One owner per behavior:** LiteLLM handles provider protocols; the local route desk owns saved default selection.
- **Explicit route choice:** a named model uses its configured route; the default alias follows the saved selection.
- **State separation:** production and staging mutable state are separate; separate ports alone do not prove isolation.
- **Evidence before guarantees:** no controlled performance, cost, reliability, or security evaluation is claimed.

## 3. Research values and evidence

Research values are observable routing, explicit state boundaries, least privilege, and reproducible failure handling. Repository configuration documents the architecture but does not constitute an independent security audit or service-level benchmark.

A validation study should test protocol compatibility, explicit/default selection, persisted switching, provider errors and retries, spend-log attribution, and production/staging isolation. Security review should examine loopback binding, credential mounts, and log redaction.

## 4. Limitations and conclusion

Provider subscription behavior and local bridges can change as vendors update clients, accounts, and terms. Compatibility is not an endorsement or guarantee of continued support. No cost, uptime, or security certification is claimed.

Subroute's design choice is clear ownership: LiteLLM handles standard API routes while local components handle specific selector and subscription behavior. Reproducible compatibility and isolation evidence remains to be gathered.

## References

[R1] LiteLLM. Proxy documentation. URL: https://docs.litellm.ai/docs/proxy/quick_start
[R2] OpenAI. Chat Completions API reference. URL: https://platform.openai.com/docs/api-reference/chat
[R3] Chen, S. Subroute repository. URL: https://github.com/stancsz/subroute