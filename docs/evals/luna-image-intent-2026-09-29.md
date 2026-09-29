# Luna image intent exception

The user clarified that image generation must be an exception to every routing mode when the request expresses image intent or explicitly needs the image tool. Restricting the exception to `/images/generations` did not satisfy that requirement.

The request hook now recognizes direct English/Chinese image requests in the latest user turn, hosted `image_generation` declarations, and explicit image-tool selections. It routes them to Codex subscription GPT-6 Luna before Force, alias, off, or auto resolution. The handler provides the hosted tool, using the existing credentials. This does not modify persisted policy and skips the saved advisor. Other tools and conversation inputs retain LiteLLM's translations. Image-named custom functions are deliberately translated to the hosted tool when selected or accompanying image intent; their arbitrary custom schemas are not hosted-image options. Merely listing such a custom function does not reroute unrelated work.

## Existing mechanism and maintenance cost

LiteLLM owns HTTP endpoints, public SSE, authentication, Router dispatch, provider transport, and standard Chat/Responses/Messages conversion. The inspected 1.101.0 and deployed 1.103.0 bridges drop hosted image tools when translating Responses to Chat. `CustomLLM` has no native Responses implementation. The existing Images bridge therefore now shares its single native `litellm.aresponses` collector with conversation requests. No service, dependency, configuration switch, store, classifier call, retry, or fallback was added.

The additional maintenance burden is a local intent matcher and a bounded compatibility adapter. Responses inputs are captured before the lossy bridge and reinstated at the provider boundary. The collector retains completed output items even when the provider's final output is empty, requires a completed terminal response naming GPT-6 Luna, validates image bytes, and closes the native stream on failure or cancellation. Existing limits remain two active image requests per process, 180 seconds, one image, and 20 MiB per image.

Native LiteLLM helpers preserve input/tool/usage conversion. SDK message objects avoid its raw-dict callback dropping later caption blocks. Two narrowly scoped output workarounds preserve captions alongside images: separate internal choices for buffered Responses, and retaining already-emitted output items in the final Responses SSE snapshot. Retire these shims when LiteLLM preserves hosted Codex images and accompanying outputs natively; regression tests exercise both dependency versions.

## Public output contract

- Chat: image data URLs in `message.images` or `delta.images`.
- Responses: base64 in `image_generation_call.result`, including the final SSE snapshot; captions are retained.
- Messages: Markdown image data URLs in text blocks. Rendering requires client support.
- SSE waits for completed generation before emitting image content. It does not claim progressive generation.
- Refusals remain text. Other returned function calls remain executable client tool calls, with no fabricated image. Unsupported/empty completion output fails visibly.

Direct image intent with no explicit tool choice selects the image tool. Caller `auto` allows other tool work first. An incompatible `none`, multiple completions, or fixed hosted-tool dimensions is rejected before provider work. The local detector is intentionally conservative, not a universal semantic classifier. Explicit hosted tools cover unrecognized phrasing. Quotes, code, older turns, tool results, image analysis, and explanatory questions are excluded by the tested rules. Previous image intent is not automatically replayed on later turns. Images edit/variation endpoints remain unimplemented. Codex's documented omission of client output-token caps remains unchanged.

## Live evidence

All four PNGs were decoded, hashed, opened, and visually inspected: an orange fox reading a blue book under a tree. Each request asked for model `auto`, proving the exception also bypasses the auto chain. The adapter checks the upstream completed model is `gpt-6-luna`; LiteLLM may echo the caller's `auto` alias in public Chat/Messages model fields.

| Instance and request | Result | Receipt |
| --- | --- | --- |
| Staging, Force MiniMax, natural image intent, buffered Chat | 38.919 s; PNG 1254 × 1254; 2,952,292 bytes | [Receipt](luna-intent-4005-chat-buffered-2026-09-29.json), [image](luna-intent-4005-chat-buffered-2026-09-29.png) |
| Staging, Force MiniMax, explicit hosted tool, Responses SSE | 41.402 s; PNG 1312 × 1199; 3,155,392 bytes | [Receipt](luna-intent-4005-responses-stream-2026-09-29.json), [image](luna-intent-4005-responses-stream-2026-09-29.png) |
| Staging, Force MiniMax, natural image intent, Messages SSE | 59.878 s; PNG 1254 × 1254; 3,167,808 bytes | [Receipt](luna-intent-4005-messages-stream-2026-09-29.json), [image](luna-intent-4005-messages-stream-2026-09-29.png) |
| Production, Force OpenRouter/MiniMax, natural image intent, Chat SSE | 50.826 s; PNG 1312 × 1199; 3,126,860 bytes | [Receipt](luna-intent-4000-chat-stream-2026-09-29.json), [image](luna-intent-4000-chat-stream-2026-09-29.png) |
| Production, “Do not generate an image. Reply only TEXT_ROUTE_OK.” | 1.749 s; provider `Minimax`; expected text; zero retries/fallbacks | [Receipt](luna-intent-production-text-2026-09-29.json) |

Production policy remained version 69 throughout. Staging was temporarily changed from Luna/high to MiniMax for the test and restored to Luna/high; all saved policy fields match the original, with version advancing 474 → 476. Gateway and gateway-staging were restarted using the existing pinned image and source mounts. Both readiness endpoints returned HTTP 200 with database connected.

The text probe initially assumed public `model` would expose MiniMax, then assumed an API-base header would be present. Both probe assertions were incorrect: LiteLLM echoes `current` and does not expose that header here. The saved successful response's explicit `provider: Minimax`, content, and unchanged policy verify the route without an additional provider call. These were probe assumptions, not gateway failures.

Responses reported 2,273 input / 76 output tokens, and Messages reported 2,344 / 92. These are Luna tokens, not image-model usage or total generation cost. Production Chat SSE omitted public usage because the request did not ask for `stream_options.include_usage`. No total-cost claim is made. The receipts omit image base64 and retain the standalone PNG hashes.

## Regression evidence

- Locked suite on LiteLLM 1.101.0: **347 passed, 11 skipped**. Live tests remain opt-in.
- Targeted suite on deployed LiteLLM 1.103.0: **105 passed**. These runs include the final multi-block-caption regression.
- Actual proxy HTTP tests cover all three protocols, buffered and SSE, natural intent and explicit tools, arbitrary/auto model inputs, Force/alias/off precedence, caption plus image preservation, other function tools, refusal, context history, conflicting options, and server-owned request context.
- Shared collector tests cover incomplete/missing terminal state, wrong model, invalid image, deadline, cancellation cleanup, and concurrency recovery. Live cancellation and client-specific Markdown/data-URL rendering were not tested.
- The live calls above preceded the final SDK-message adjustment for multi-block captions; that edge case is covered by the added regression, and the final source is restarted in both containers.

Commands: `uv run --locked --extra test pytest -q --disable-warnings` and `uv run --locked --extra test --with 'litellm[proxy,extra-proxy]==1.103.0' pytest -q tests/test_image_intent.py tests/test_codex_images.py tests/test_codex_proxy_http.py tests/test_dynamic_routing.py --disable-warnings`.
