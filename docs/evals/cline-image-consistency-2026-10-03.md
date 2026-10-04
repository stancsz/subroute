# Cline image consistency repair

## Findings

Cline Desktop 0.0.43 showed image captions without an image or a tool activity row. The saved sessions contained only text messages. This alone does not establish whether the provider generated an image: Subroute's automatic image-intent route could recognize Cline's whole-message user envelope and return Chat `delta.images`, which Cline's compatible chat renderer does not display. That interception competes with the installed MCP image tool.

The installed image MCP server was enabled and correctly configured. Its `generate_image` function calls the local Images endpoint with `model=codex-luna`, independently of the conversation's selected model. The endpoint and hosted image handler enforce GPT-6 Luna even under Force, Alias, and Off policies. Existing FastMCP conversion returns native MCP image content.

The installed Cline SDK defaults to a 60-second MCP request timeout. This is shorter than Subroute's 180-second image deadline and the MCP HTTP client's 190-second deadline. It is a confirmed configuration mismatch; these records do not prove that a specific failed request timed out.

## Repair

- Requests advertising `subroute-image-generation__generate_image` keep the client function definition and native tool choice. Automatic intent interception is skipped so the chat model can request the client tool. Explicit hosted image generation still uses the Luna path.
- The image MCP tool's separate Images request continues to use Luna. Saved chat routing and advisor settings are not changed by image generation.
- Added the global Cline rule `Documents/Cline/Rules/image-generation.md`, requiring exactly one MCP call for a direct image-creation request and prohibiting claims of success without a successful result.
- Set the image server's Cline MCP `timeout` to 240 seconds. Existing transport, authentication environment, and other server registrations were preserved.

This adds no service, dependency, retry, or model fallback. The routing exception is specific to the registered Cline image function. It can be retired if Cline renders the native compatible Chat image field reliably.

## Evidence

The existing Python environment ran `tests/test_image_intent.py`, `tests/test_codex_images.py`, and `tests/test_image_mcp.py`: **153 passed**, 17 library warnings. New regressions cover Chat, Responses, and Anthropic Messages tool schemas, three saved policy modes, two active chat models, automatic and explicitly selected client tools, and precedence for explicit hosted tools. Existing endpoint tests verify Luna image dispatch across saved policies and caller model labels. These are provider fixtures, not live provider calls.

Six additional checks exercised LiteLLM's actual HTTP routes with fixture provider responses. Both complete and streamed replies preserved `subroute-image-generation__generate_image` and its arguments across all three protocols. The chat dispatch used the saved `codex-terra` route, demonstrating that advertising the image function no longer switches the conversation to the internal Luna image handler. Nonstreamed replies retained native `tool_calls`, Responses `function_call`, and Anthropic `tool_use` structures and terminal reasons.

The unchanged API gateway passed `npm run test:compatibility`, including local-first function-call streaming and official OpenAI/Anthropic SDK tool-response checks. This covers gateway protocol preservation with fixtures; it does not establish current deployed Cline behavior. `git diff --check` passed for the changed tracked files.

The running Cline sidecar's read-only settings API reported the image server enabled and valid. Its instruction discovery found the global image rule with zero warnings. The server-list response omits timeout fields; installed schema inspection confirms server-level `timeout` maps to `timeoutSeconds` and is used for tool execution and transport requests.

An actual MCP stdio initialization using the configured Python executable and server arguments took 0.87 seconds and advertised `generate_image`. It did not invoke generation. The saved settings value was verified as 240 seconds. This successful startup does not establish that earlier cold startups exceeded Cline's default connection deadline.

## Release boundary

The owner authorized restarting production and staging. Both `gateway` and `gateway-staging` restarted successfully on October 3 at 9:15 PM America/Denver (October 4, 03:15 UTC). Both loopback readiness endpoints returned HTTP 200 after startup, on ports 4000 and 4005 respectively. This reloads the changed source module. Initial readiness attempts during startup closed without an HTTP response; subsequent bounded polling succeeded without another restart.

The approval request also offered up to two live Cline image checks, but the owner's reply authorized the restarts only. No paid inference call was made during this repair check. A fresh Cline session is needed to exercise the new global rule and refreshed MCP registration.

Live tool invocation, generated image delivery, and rendering in Cline 0.0.43 remain to be verified. Cline may place the image inside the expandable tool activity, while the assistant's final message remains text. An instruction does not guarantee that every chat model will select a tool; the deterministic guarantee is that an invoked image tool routes generation to Luna.
