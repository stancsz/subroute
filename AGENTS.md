# Working agreements

Write in a natural tone. Do not use em dashes.

## Keep the gateway small and maintainable

Avoid bloat. Choose the smallest clear design that satisfies the current requirements in GOAL.md. Maintainability means clear ownership, predictable behavior, and few moving parts, not the lowest line count. Do not remove required features merely to make the code smaller.

Docker Compose is the intended deployment model. Keep it simple and maintainable. Container count alone is not evidence of bloat; preserve the gateway, PostgreSQL, and staging roles where they serve current requirements. Do not replace Docker with host processes as a debloating measure.

Prioritize debloating by fragility and impact: duplicated protocol implementations, silent request mutations, competing policy owners, shared mutable state, unbounded provider work, and dependency drift come before cosmetic cleanup. For each proposed simplification, identify the failure mechanism it removes and the behavior it must preserve. Prefer small changes with targeted regression evidence over broad rewrites. Extra validation or cleanup code is justified when it prevents a concrete failure.

- Let LiteLLM own public protocols, streaming, standard provider adapters, retries, and advisor orchestration. Before adding a custom implementation, inspect the installed LiteLLM capability and establish the concrete gap. Keep any necessary workaround narrow and document its reason and retirement condition.
- Justify each new service, dependency, persistent store, configuration option, abstraction, and custom transport with a current use case. Prefer existing mechanisms. Do not build frameworks or extension points for hypothetical future needs.
- Prefer straightforward configuration and small functions. A few explicit model aliases are acceptable; do not replace simple repetition with a generator, registry, or inheritance hierarchy unless it reduces actual maintenance work.
- Give each setting one authoritative owner and document precedence between defaults, environment variables, and persisted state. Resolve a request's routing and advisor policy consistently. Avoid hidden singleton imports and import-time disk writes or route registration where practical.
- Keep provider transport, routing policy, persistence, and presentation responsibilities clear. Split modules when it clarifies ownership or testing, not to meet arbitrary file-size targets. Keep the existing model switcher lightweight.
- Keep production and staging mutable state separate. Separate ports alone do not provide isolation. Shared code, configuration, credentials, or databases must have explicit, understood consequences. Optional services should not become mandatory without a current requirement.
- Preserve request semantics. Do not silently discard content, change instruction roles, remove limits, or fabricate successful completion to make an adapter work. Reject unsupported inputs or document and test a deliberate translation. Distinguish estimated usage from provider usage.
- Bound custom provider work with timeouts and cancellation cleanup. Validate terminal outcomes before reporting success. Avoid hidden retries, fallback calls, or additional provider spending.
- Keep dependency versions reproducible across development, tests, and deployment. Do not rely on a mutable latest image for reproducible releases. Verify framework hook ordering and protocol behavior against the deployed version.
- Test behavioral boundaries and failure paths, especially custom transport completion, cancellation, request translation, policy consistency, and environment isolation. Mocked unit tests do not establish live provider compatibility. Do not add tests that merely restate implementation details.
- Remove obsolete code and documentation within the scope of a change. Preserve unrelated user changes. Keep README.md, GOAL.md, and runtime behavior consistent; never redefine acceptance criteria to match an incomplete implementation.

For design reviews and meaningful additions, explain the requirement, existing mechanism considered, new maintenance burden, and evidence that the behavior works. Distinguish confirmed defects from design risks and optional simplifications. Do not treat a feature as unused without evidence.
