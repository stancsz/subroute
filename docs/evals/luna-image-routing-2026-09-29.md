# Luna image routing verification

Historical endpoint-only verification. The later [intent-routing extension](luna-image-intent-2026-09-29.md) supersedes the final scope limitation below and adds conversation/tool detection.

Every request to the Images generation endpoint now selects `codex-luna` before the saved text routing policy, including Force and explicit `auto`. Luna calls the hosted image-generation tool through the existing Codex subscription credentials. There is no API-key deployment, advisor consultation, retry, or fallback on this path.

The requirement came from the user's 2026-09-29 request to keep image generation on Luna regardless of Force selection. The previous staging request failed with `Not implemented yet`; production Force routed it to MiniMax and returned HTTP 200 with an empty image list.

## Existing mechanism and narrow workaround

Inspected the deployed LiteLLM 1.103.0 implementation. `CustomLLM.aimage_generation` raises `Not implemented yet`; its Responses-to-Chat conversion explicitly drops `image_generation` hosted tools. The native ChatGPT adapter also uses a different authentication-file schema, adds coding instructions, and filters request parameters. It is not a drop-in replacement for the existing subscription boundary.

The new `codex_images.py` implements only Images-to-Responses translation and image-result validation. LiteLLM still owns the public Images endpoint, authentication, Router dispatch, Responses HTTP transport, SSE parsing, and error envelopes. It adds no dependency, service, persistent state, configuration switch, or alternate credential store. Retire the bridge when LiteLLM natively supports this Codex subscription Images operation with equivalent request/result preservation.

Codex emits the image in `response.output_item.done` and may leave `response.completed.output` empty. The adapter retains that item, requires a completed terminal response naming GPT-6 Luna, and rejects missing/malformed/non-image results. Work is limited to two concurrent image requests per gateway process, a 180-second deadline, a single returned image, and a 20 MiB image payload. Stream connections and slots are released on cancellation and failure. LiteLLM's `cancel_on_disconnect` setting is enabled for buffered requests. Cancellation cleanup has injected test evidence; disconnect behavior has not been tested against a real image provider mid-generation.

## Live results

| Check | Result |
| --- | --- |
| Staging, temporary Force target `minimax`, request model `auto` | Real Luna image, 33.743 seconds, PNG 1254 × 1254, 1,129,736 bytes. Previous staging policy fields restored; policy version advanced from 472 to 474. |
| Production, existing Force target `openrouter`, request model `current` | Real Luna image, 36.042 seconds, PNG 1254 × 1254, 1,198,099 bytes. Production policy v69 unchanged. |
| Fixed size, n=2, URL response on both instances | All six requests returned HTTP 400. Unit/HTTP fixtures verify unsupported requests do not require provider generation. |
| Ordinary production Chat request using `current` | MiniMax returned `TEXT_ROUTE_OK`, finish reason `stop`, 184 input and 33 output tokens. Initial 20-token probe returned null content; the explicit 512-token rerun succeeded. |
| Production and staging readiness after restart | HTTP 200 on both. |

Both generated PNGs were opened and visually inspected. [Production image](luna-image-production-2026-09-29.png), [production receipt](luna-image-production-2026-09-29.json), [staging image](luna-image-generation-2026-09-29.png), [staging receipt](luna-image-generation-2026-09-29.json).

Luna reported 2,318 input / 67 output tokens in production. These counts are not image-model usage or proof of total cost. The response exposes `luna_usage` separately, leaves `image_usage` and standard Images usage unavailable when the provider omits them, and removes LiteLLM's added Luna-only cost estimate.

## Regression evidence and limits

- Locked local suite, LiteLLM 1.101.0: **292 passed, 11 skipped**. Live tests remain opt-in.
- Targeted image, HTTP protocol, routing and UI tests against LiteLLM 1.103.0 with proxy extras: **51 passed**. The first isolated attempt without proxy extras could not collect because `httpx2` was missing; the corrected environment passed. Existing development/deployment version differences are explicitly tested, not assumed identical.
- Coverage includes Force/alias/off precedence, `auto`/arbitrary/omitted model inputs, option preservation at the real proxy HTTP boundary, empty terminal output, missing/incomplete terminal responses, wrong model, malformed image payloads, timeout, cancellation, concurrency rejection and recovery, and separation of actual token counts from estimated cost.

The live subscription tool returned 1254 × 1254 even when asked for 1024 × 1024. Fixed dimensions are therefore rejected before dispatch; callers must omit `size` or use `auto`. PNG response dimensions are read from the image bytes. The endpoint supports one complete base64 image, not URL hosting or streamed Images responses. Other provider tool options are forwarded and remain provider-dependent.

The routing rule applies to `/images/generations` and `/v1/images/generations`, not semantic classification of chat prompts, image edits/variations, or hosted image tools submitted through the public Responses-to-Chat bridge. Those public Responses tools remain unsupported; use the Images endpoint. Ordinary text and Advisor behavior keeps its existing owner and policy.
