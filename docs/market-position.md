# Subroute: market position and maturity

**Reviewed:** 2026-11-23<br>
**Product readiness:** single-operator self-hosted beta. The local production/staging configuration and client contracts are exercised in one environment; independent deployment, security, and broad provider compatibility are not established.<br>
**Market validation:** early open-source visibility only. Public stars and forks do not confirm repeat use, customer demand, or willingness to pay.

## Audience and job

The intended user is a developer who uses several coding clients and wants one local API endpoint for selected subscription adapters and configured LiteLLM API routes. The job is to make default-route choice visible and persistent while keeping provider protocol behavior, retries, fallbacks, and usage handling with LiteLLM.

## Alternatives and position

| Substitute | What its first-party source establishes | Subroute's testable position |
| --- | --- | --- |
| LiteLLM Proxy by itself | LiteLLM documents provider routing, load balancing, retries, and fallbacks. | Subroute packages a local route desk and maintains Codex and Antigravity subscription adapters alongside LiteLLM-managed API routes. It should demonstrate dependable client compatibility and clear state ownership against a plain LiteLLM configuration. |
| Managed model-routing services | Simplify provider selection and access without self-hosting the gateway. | Subroute keeps gateway state local to the operator and routes explicit user-selected subscription paths. It carries more setup and operational responsibility. No cost or privacy advantage has been independently measured. |
| Native client-specific model configuration | Low setup when a developer uses one client/provider. | A shared endpoint may reduce repeated configuration across clients, but compatibility and switching benefits need a multi-client task study. |

The main infrastructure substitute is [LiteLLM Router](https://docs.litellm.ai/docs/routing), which already implements provider routing and fallback behavior. Subroute deliberately leaves those responsibilities there rather than claiming a new general-purpose router.

## Evidence and limits

- The repository includes client stream/schema contract tests, explicit production/staging databases and route files, and a local route desk. These are implementation and single-environment checks.
- The local deployment was exercised on one operator environment. There is no controlled reliability, cost, privacy, security, or compatibility evaluation across independent users and deployments.
- GitHub snapshot on 2026-11-23: 7 stars and 4 forks. The repository had no published non-draft release; the visible `v1.0.0` entry was a draft/untagged release. This is a small open-source visibility signal, not an adoption or quality measure.

## Adoption friction and next experiment

Users need Docker Compose, provider credentials, client-specific configuration, database and state handling, and trust in subscription-adapter compatibility. A route mistake or shared-state error can cause cost, data, or availability problems. The next decision-changing experiment is an independent operator compatibility matrix across the supported clients and adapters, with repeated streaming, tool, image/audio, failure, cancellation, usage-attribution, and production/staging isolation cases. Record exact versions and external review findings before making a broad beta or security claim.

## Product and research values

- **One owner per behavior:** LiteLLM owns provider protocols and retries; the route desk owns saved default selection.
- **Explicit route choice:** a named model uses its configured route; `default` follows the saved selection.
- **State separation:** distinct production/staging databases and files are tested as behavior, not inferred from separate ports.
- **Least exposure:** keyless listeners bind to loopback in the documented local configuration.
- **Evidence before guarantees:** no controlled performance, cost, reliability, security, or market-fit result is claimed.

## Sources

- [Project README](../README.md)
- [Client contract tests](../tests/test_client_stream_contracts.py)
- [Shared PostgreSQL operations](operations/shared-postgres.md)
- [LiteLLM routing](https://docs.litellm.ai/docs/routing)
