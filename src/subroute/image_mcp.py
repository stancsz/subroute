"""Cline MCP image tool backed by Subroute's existing Images endpoint."""

from __future__ import annotations

import argparse
import base64
import binascii
import os

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations

MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TIMEOUT = 190


def decode_image(value: object) -> tuple[bytes, str]:
    if not isinstance(value, dict) or not isinstance(value.get("b64_json"), str):
        raise ValueError("Subroute returned no base64 image")
    encoded = value["b64_json"]
    if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 4):
        raise ValueError("Generated image exceeds 20 MiB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Subroute returned invalid base64 image data") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Generated image exceeds 20 MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, "image/webp"
    raise ValueError("Subroute returned an unsupported image format")


def create_server(gateway_url: str = "http://127.0.0.1:4000", api_key: str | None = None) -> FastMCP:
    server = FastMCP("subroute-image")
    annotations = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=annotations, structured_output=False)
    async def generate_image(prompt: str) -> Image | TextContent:
        """Generate one image and return it as inline image content in Cline. Use for direct image-creation requests."""
        prompt = prompt.strip()
        if not prompt or len(prompt) > 8000:
            raise ToolError("Image prompt must contain 1 to 8000 characters.")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT, follow_redirects=False) as client:
                response = await client.post(
                    gateway_url.rstrip("/") + "/v1/images/generations",
                    headers=headers,
                    json={"model": "codex-luna", "prompt": prompt, "n": 1,
                          "response_format": "b64_json", "size": "auto"},
                )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("data"), list) or len(result["data"]) != 1:
                raise ValueError("Subroute returned an unexpected Images response")
            image, mime_type = decode_image(result["data"][0])
            return Image(data=image, format=mime_type.removeprefix("image/"))
        except httpx.TimeoutException:
            message = "Image generation exceeded 190 seconds; no retry was made."
        except httpx.HTTPStatusError as exc:
            message = f"Subroute image generation failed with HTTP {exc.response.status_code}."
        except httpx.RequestError as exc:
            message = f"Subroute image service is unavailable ({type(exc).__name__}); no retry was made."
        except (ValueError, KeyError, TypeError):
            message = "Subroute returned an invalid image response."
        raise ToolError(message)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:4000")
    args = parser.parse_args()
    create_server(args.gateway_url, os.getenv("SUBROUTE_API_KEY")).run(transport="stdio")


if __name__ == "__main__":
    main()
