# Subroute dashboard and shared Badlands identity

Owner objective, 2026-10-11: redesign Subroute to look and feel like LiteLLM,
design a minimalist Badlands logo, and replace both websites' branding.
Branding scope is Subroute, the Badlands gateway console and company site.

## Plan and acceptance

Use the official [LiteLLM Admin UI reference](https://docs.litellm.ai/docs/proxy/ui):
a compact neutral sidebar, thin breadcrumb/status bar, white forms and ruled
provider rows. Keep Routing, Connections and Desktop-only coding agents. Link to
existing LiteLLM administration for its keys/models/logs. Preserve local routing
policy, target/advisor reasoning, autosave, provider-reported quota and explicit
unavailable states. Mobile uses a labeled collapsible navigation and stacked forms.

The shared original Mesa B is a single-color geometric mark with a left spine,
two stepped shelves and open counters. Use identical geometry in Subroute and
Badlands headers, footers, sign-in and favicon; keep product/company names as text.
Verify small-size clarity, both polarities, full company name and narrow headers.

Acceptance requires actual desktop/narrow rendered views; routing/advisor saves
and reload in an isolated fixture; invalid save and recovery; quota/inventory
failure; menu/keyboard navigation; Desktop bridge presentation without real launches;
existing backend/control checks; independent nonauthor review and final publication
verification. Provider inference and live routing mutations are not required.

## Evidence and current progress

The Subroute source is mounted read-only into the running Docker services. HTML,
CSS and JS presentation edits are visible without restarting inference services.
The fixture at loopback 64921 uses actual sanitized routing/inventory schemas with
illustrative quota, its own persisted policy and explicit failure flags. It does
not proxy provider requests or mutate production/staging policy.

Image-backed GPT-6.1 Sol direction and repair review completed: neutral layout,
Mesa B geometry, native-size logo/favicons at DPR1/2, company headers/footers,
complete Routing/Connections, phone/tablet/lower/expanded inventory, saved/rejected
policy, offline/quota/empty, Desktop connection/agent states and console lockups.
Eight calls reported 50,249 tokens. Material repairs included tablet clipping,
fractional breakpoint coverage, heading focus after navigation, policy feedback
at the heading, explicit offline policy and readable agent retry/picker labels.
Final scoped visual verdict passed all inspected regions. Native operating-system
select popup appearance remains unverified because browser capture omits it.
Native option selection and persistence were exercised; no fabricated popup proof.

Independent nonauthor QA passed actual fixture target/advisor/effort saves and
reload, rejected-save preservation/recovery, initial offline and recovery,
partial quota failure, empty inventory, phone menu/Escape/focus, fractional
767.5px and tablet rows, simulated Desktop chooser/launch/install/recheck,
provider-pending status and loopback connection errors. Real native process
launch was outside this UI review. See [independent review](badlands-redesign-review.md)
for exact source hashes, tasks, images and limits. Automated checks passed:
19 control/routing tests, five Desktop launcher tests and 14 real console
Worker/SQLite/auth runtime tests. Two Python dependency deprecation warnings
remain. No provider inference was run for this redesign.

Branding was published to the company Worker `badlandslabs-site`, version
`6b255578-420f-4bf1-b3ec-71a0d59f31df`, and separate console Worker
`badlands-gateway-console`, version `eef1279f-ef3e-42b7-891b-6fa4eeea22d3`.
Company assets were prepared from actual live public HTML plus logo-only edits,
preserving live copy/catalog and the complete prior asset set. Its Worker source
was downloaded and retained byte-for-byte (SHA256
`494ede1a430e05c36b232bb8f629639a7a866f757477b3acf002b48f07b5d953`).
An initial Wrangler no-bundle release auto-discovered extra scratch modules;
this was repaired by publishing from an isolated directory with
`find_additional_modules: false`. API readback confirms exactly one Worker
module, identical code and bindings. Public asset bytes match the reviewed
release. Unauthenticated admin returns401, console redirects to sign-in, and
the authenticated console reconnects to staging and production.

Both live local Subroute environments serve the new UI with existing MiniMax M3,
GPT-6 Luna advisor and Force policy intact: production v98 and staging v627.
The UI source mount requires no inference-service restart. Branding-only site
changes are staged atop HEAD, leaving unrelated business/admin/router edits in
the working trees. Seven non-overlapping tracked files were hash-verified unchanged.

Representative evidence: [logo proof](../brand/proofs/logo-dpr1.png),
[fixture routing](../brand/proofs/subroute-routing.jpg),
[fractional-width inventory](../brand/proofs/subroute-tablet.jpg).
Fixture quota values are explicitly illustrative; production never receives
that fixture notice. Canonical mark and usage guidance are in [brand](../brand/README.md).

Status: implementation, independent review and publication complete. Delivery
revisions are recorded in the scoped repository commit history. Scratch and raw
advisor receipts remain ignored in `tmp/badlands-redesign/`.
