# Optional HTTP clients

Run tools from this checkout while the Subroute gateway is listening.

```powershell
uv run --extra music python -m clients.image_mcp
uv run --extra music python -m clients.music_mcp --root <audio-directory>
```

The image client requests `codex-luna` through Images. The music client
uses `gemini-subscription` with explicit audio attachments.
