# Architecture

```mermaid
flowchart LR
    Clients[Codex / Claude Code / coding tools] --> Proxy[LiteLLM Proxy production :4000]
    Proxy --> Native[Native LiteLLM providers]
    Native --> OpenRouter[OpenRouter]
    Proxy --> Compatible[OpenAI-compatible endpoints]
    Proxy --> Ollama[Native Ollama provider]
    Proxy --> Antigravity[In-process Antigravity CustomLLM]
    Proxy --> Advisor[Advisor injection callback]
    Control[Local control plane /control] --> State[Atomic active_model.json]
    State --> Router[In-memory alias resolver]
    Router --> Proxy
    Advisor --> BuiltIn[LiteLLM AdvisorOrchestrationHandler]
```

LiteLLM Proxy is the only public protocol engine. Standard provider translation
stays in LiteLLM. The narrow Codex advisor adapter collects upstream Responses
SSE into a completed text response and validates terminal status and usage.

## Ownership

LiteLLM owns:

- HTTP endpoints used by coding tools
- streaming and tool-event protocol behavior
- standard provider authentication and transport
- model deployments and key selection

This repository owns:

- a small, reviewable LiteLLM configuration
- explicit public model aliases
- fail-closed retry and fallback settings
- acceptance tests that prevent protocol and provider abstractions from
  growing back into the project
- a callback that injects LiteLLM's built-in Advisor tool on the Anthropic
  Messages path whenever the captured policy contains an advisor selection
- a loopback-only routing control plane that resolves `current`, or explicitly
  applies `force` mode, before LiteLLM selects a deployment

The dynamic routing callback precedes the Advisor callback. A request for
`current` resolves to the selected model, and the same policy snapshot decides
whether to consult an advisor. An empty advisor selection disables consultation. Rewrites carry requested and resolved model names, routing mode, and
policy version as audit metadata. Existing in-flight requests are never
retargeted.

The Codex advisor boundary preserves valid tool history by translating
Anthropic and OpenAI Chat tool exchanges into Responses input items. Advisor
injection is skipped before orchestration when the selected advisor cannot
represent that history. This keeps the executor request intact and leaves the
provider handler as the final fail-closed validation boundary.

Provider-specific Python belongs here only when LiteLLM has no native provider
or OpenAI-compatible seam. Antigravity's registered handler launches a bounded
external CLI process. Ordinary Codex subscription requests use LiteLLM's
Responses bridge; a deployment hook reloads local credentials and applies
instruction-role compatibility only for the exact Codex backend URL. The
advisor needs upstream streaming despite a non-streaming orchestration call,
so it retains a narrow collector until the standard bridge supports that
combination. See tests/test_codex_bridge.py for the dispatch regression.

Each request resolves one executor/advisor policy snapshot. Persisted advisor
selection takes precedence over the ADVISOR_MODEL startup default. Production
and staging use distinct mutable policy storage and PostgreSQL clusters within
Docker Compose. Static source/configuration remain shared and require review
before a production restart.

## Evidence boundary

Config tests prove only the intended ownership boundary. LiteLLM startup proves
only that the proxy can load. Coding-tool compatibility requires the real
LeanRouter protocol fixtures and live Codex and Claude Code tool/stream tests.
