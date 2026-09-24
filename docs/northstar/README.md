# Product direction

Subroute is a local AI subscription router for developers using coding tools. Its job is to keep the client endpoint stable while the person chooses which configured model handles new requests. The repository describes local provider credentials, explicit routing, and visible failure behavior as core product boundaries; see [README.md](../../README.md) and [GOAL.md](../../GOAL.md).

The current customer thesis is a single developer who wants to use several AI subscriptions and API connections from existing coding tools without changing each tool's model setting. The intended measurable value is fewer client reconfigurations and clearer control over where a request goes. The repository does not establish a paid business model, supported installer/distribution channel, or commercial support promise. This UI task does not invent those decisions.

## Interface standard

The control desk should feel like a considered local developer product, not an operator prototype. Keep the routing action obvious, show actual gateway and provider state, put configured connections ahead of setup choices, and never make unavailable quota look healthy. Browser and Desktop share the same routing and provider interface. Desktop adds only a friendly local connection and coding-agent launch experience.

Current visual direction: warm paper, deep evergreen, and a restrained signal-lime accent, with clear type hierarchy and local system fonts. Keep the interface useful at laptop and narrow browser widths. Keep claims tied to the gateway's loopback boundary and provider-reported data.

## Product reference

On 2026-09-24, reviewed the [LiteLLM Proxy quickstart](https://docs.litellm.ai/docs/proxy/docker_quick_start) and [gateway overview](https://docs.litellm.ai/docs/) as the strongest direct product reference because Subroute is built on LiteLLM for the same self-hosted gateway workflow. This was a documentation review, not a hands-on competitor UI inspection. LiteLLM documents a first-run journey for connecting a provider, adding and testing a model, sending a Playground request, and creating a client key. Subroute's current control desk has a narrower job: local policy and subscription routes around the LiteLLM gateway. Keep those areas focused, and close the commercial onboarding gap with a clear setup path rather than a second provider administration system.

Reference links: [LiteLLM overview](https://docs.litellm.ai/docs/) and [Docker quickstart](https://docs.litellm.ai/docs/proxy/docker_quick_start).
