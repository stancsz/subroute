# Cline Desktop image delivery investigation

## Failure and root cause

Cline Desktop 0.0.40's OpenAI-compatible chat session did not render the Subroute `delta.images` response field. The saved session first returned only captions and a Markdown `attachment` placeholder. Cline Desktop can display images returned by MCP tools, so a small stdio MCP bridge was added to call Subroute's existing `/v1/images/generations` endpoint.

The first MCP implementation returned an `mcp.types.CallToolResult` from a FastMCP-decorated function. FastMCP treated that result as a function value and serialized it into a text block containing JSON, including several megabytes of Base64. The actual Cline session showed `tool_result.content` beginning `{"content":[{"type":"image"...`; the subsequent assistant turn called the image Base64 corrupt. This was a content-shape bug, not corrupt provider output: strict Base64 decoding succeeded, the PNG header and IEND were valid, and the recovered 1536x1024 image rendered successfully.

The tool now returns FastMCP's native `Image(data=..., format=...)` helper. The decorator explicitly disables structured output so the SDK converts that helper to one top-level MCP `ImageContent` block instead of JSON text. Failure paths raise `ToolError`, preserving tool failure signaling without returning a nested result object. Prompt limits, image format checks, one request per tool call, 190-second client timeout, and no retries remain unchanged.

The existing intent matcher also recognizes only a whole-message Cline `<user_input mode="act|plan|yolo">...</user_input>` envelope for routing classification. It forwards the original message unchanged and does not unwrap arbitrary XML or quoted examples.

## Evidence

- `tests/test_image_mcp.py`: 4 passed. It verifies strict image format decoding, MCP conversion to a native image content block, bounded prompt handling, and visible upstream failure without retry. Pytest emitted one existing Pydantic settings forward-reference warning from the installed MCP stack.
- A real MCP stdio client call against the tool process and a local Images API fixture returned one top-level `type=image`, `mimeType=image/png` content block. Its Base64 decoded to the exact 3.43 MB PNG fixture. This wire-level check specifically guards against the nested JSON text regression.
- The Cline Desktop session's tool result from the reproduction contained a valid 3.43 MB PNG. It is preserved locally at `tmp/cline-mcp-output.png` for visual inspection; Cline received it as JSON text before the fix.
- After the code fix and Cline Desktop restart, a fresh prompt at 23:38 local time invoked `subroute-image-generation__generate_image`. The local process used `src/subroute/image_mcp.py`, returned a 2.97 MB PNG, and the decoded image rendered successfully at `tmp/cline-mcp-latest.png`. Cline's assistant text said it could not embed the image in its final text response. The user's follow-up screenshot shows the completed `Worked for 41s and made 2 tool calls` row collapsed, so the MCP result is hidden behind that disclosure in the captured view.
- The Subroute production and staging gateway services were restarted at the user's request and both readiness endpoints returned HTTP 200. Real generation through the configured `https://api.badlandslabs.com/v1` route returned one valid PNG via `delta.images` and `[DONE]`; it did not validate Cline rendering, which is why the MCP bridge was added.
- Cline Desktop's MCP settings now include `subroute-image-generation`, using this checkout's Python executable and the loopback gateway. The existing Playwright MCP entry was preserved. Cline Desktop was restarted after the FastMCP return-type fix; its MCP server starts when the tool is first called.

## Remaining boundary

The post-restart Cline request, MCP image response, and decoded PNG are verified. The screenshot confirms the tool activity row is collapsed. Expand the `Worked for … and made … tool calls` row to view the image-bearing result; Cline Desktop 0.0.20+ documents inline rendering for MCP image results. The assistant's final text may remain a text acknowledgment. The code cannot force the model to call the tool on every conversational image request, so the tool description says to use it for direct image-creation requests. No alternative provider or hidden retry was added.

## Advisor receipt

One Codex Sol advisor consultation (`codex-sol-advisor`, request `chatcmpl-codex-advisor-d64bfc065c2e`) used 549 prompt and 772 completion tokens (1,321 total; 33.476 seconds). Initial advice was to probe the chat `delta.content` shape before adding an adapter. Inspection of the actual Cline session and FastMCP's installed result conversion showed the narrow fix was instead a native MCP `Image` return; this changed the next experiment and the implementation (`decision_changed: true`).
