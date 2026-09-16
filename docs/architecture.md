# Architecture

```mermaid
flowchart LR
    Clients[Codex / Claude Code / coding tools] --> Proxy[LiteLLM Proxy staging :4005]
    Proxy --> Native[Native LiteLLM providers]
    Proxy --> Compatible[OpenAI-compatible endpoints]
    Proxy --> Ollama[Native Ollama provider]
    Proxy --> Antigravity[In-process Antigravity CustomLLM]
    Proxy --> Advisor[Advisor injection callback]
    Advisor --> BuiltIn[LiteLLM AdvisorOrchestrationHandler]
```

LiteLLM Proxy is the only public protocol engine. This repository does not
translate Chat Completions, Responses, Anthropic Messages, SSE events, tool
calls, or provider errors.

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
- an opt-in callback that injects LiteLLM's built-in Advisor tool for exact
  guided aliases on the Anthropic Messages path

Provider-specific Python belongs here only when LiteLLM has no native provider
or OpenAI-compatible seam. Antigravity is the one current exception and runs
inside the proxy process. ChatGPT subscription uses LiteLLM's own Responses
bridge; a deployment hook refreshes local credentials immediately before each
request.

## Evidence boundary

Config tests prove only the intended ownership boundary. LiteLLM startup proves
only that the proxy can load. Coding-tool compatibility requires the real
LeanRouter protocol fixtures and live Codex and Claude Code tool/stream tests.
