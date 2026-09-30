"""Two read-only listening tools using Subroute's existing Gemini Subscription."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
from pathlib import Path
import wave
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from subroute.audio import MAX_AUDIO_BYTES, decode_audio


def read_asset(root: Path, supplied_path: str) -> tuple[dict, dict]:
    path = (root / supplied_path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("audio file must stay inside the configured root")
    if path.suffix.lower() not in {".wav", ".mp3"}:
        raise ValueError("audio supports PCM WAV and MP3 only")
    with path.open("rb") as stream:
        raw = stream.read(MAX_AUDIO_BYTES + 1)
    if len(raw) > MAX_AUDIO_BYTES:
        raise ValueError("audio exceeds 20 MiB")
    attachment = {"data": base64.b64encode(raw).decode(), "format": path.suffix.lower()[1:]}
    decode_audio(attachment)
    receipt = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "format": attachment["format"]}
    if attachment["format"] == "wav":
        with wave.open(io.BytesIO(raw), "rb") as wav:
            receipt.update(sample_rate=wav.getframerate(), channels=wav.getnchannels(), duration_seconds=wav.getnframes() / wav.getframerate())
    return attachment, receipt


def create_server(root: Path, gateway_url: str, api_key: str | None = None) -> FastMCP:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("audio root must be a directory")
    server = FastMCP("subroute-music")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    async def listen(paths: list[str], question: str, focus: list[str]) -> dict:
        if not question.strip() or not focus or any(not str(item).strip() for item in focus):
            raise ValueError("question and non-empty focus are required")
        assets = [read_asset(root, path) for path in paths]
        if sum(receipt["bytes"] for _, receipt in assets) > MAX_AUDIO_BYTES:
            raise ValueError("audio attachments exceed 20 MiB combined")
        prompt = (
            "Listen to every attached audio file. "
            "For two files use labels A (attachment 1) and B (attachment 2). "
            "Report audible observations with time ranges, suspected causes separately, "
            "and limitations or things you cannot assess. Do not invent measurements, "
            "universal taste scores, or claim to have processed the audio. "
            "For comparisons, loudness is NOT matched; disclose this and do not "
            "equate louder with better. Do not estimate exact BPM or stereo width. "
            "Use the supplied WAV duration, rather than guessing it. "
            "Return only the final analysis, without internal planning. Reply in the language of the question.\n"
            f"Known input metadata (not perceptual guesses): {[{k:v for k,v in receipt.items() if k not in {'path', 'sha256'}} for _, receipt in assets]}\n"
            f"Focus: {', '.join(focus)}\nQuestion: {question}"
        )
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=150, follow_redirects=False) as client:
            response = await client.post(
                gateway_url.rstrip("/") + "/v1/chat/completions",
                headers=headers,
                json={"model": "gemini-subscription", "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    *[{"type": "input_audio", "input_audio": item} for item, _ in assets],
                ]}]},
            )
            if response.status_code >= 400:
                try:
                    error = response.json()
                    detail = error.get("detail") or error.get("error", {}).get("message") or "gateway request failed"
                except (ValueError, AttributeError):
                    detail = "gateway returned an unreadable error"
                raise RuntimeError(f"Gateway HTTP {response.status_code}: {str(detail)[:600]}")
            result = response.json()
        choice = result["choices"][0]
        if result.get("model") != "gemini-subscription":
            raise RuntimeError("gateway routed listening request to an unexpected model")
        if choice.get("finish_reason") != "stop" or not choice["message"].get("content") or not result.get("usage"):
            raise RuntimeError("gateway did not return completed text with provider usage")
        return {
            "schema_version": "gemini-listening-v1", "status": "complete",
            "backend": "gemini-subscription", "response_id": result["id"],
            "inputs": [receipt for _, receipt in assets], "observations": choice["message"]["content"],
            "usage": result["usage"],
            "limitations": ["Listening observations are not calibrated DSP measurements or professional mixing certification.",
                             "Comparison files are not loudness matched; no automatic processing or DAW edits are performed.",
                             "Audio is transmitted to Google's Subscription service; this is not offline inference."],
        }

    @server.tool(annotations=annotations, structured_output=True)
    async def analyze_audio(asset_path: str, question: str, focus: list[str]) -> dict[str, Any]:
        """Listen to a WAV/MP3 inside the configured root using Gemini Subscription."""
        return await listen([asset_path], question, focus)

    @server.tool(annotations=annotations, structured_output=True)
    async def compare_audio(a_path: str, b_path: str, question: str, focus: list[str]) -> dict[str, Any]:
        """Listen to two files as A/B. Loudness is not matched; comparisons are provisional."""
        return await listen([a_path, b_path], question, focus)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Only files inside this audio folder may be sent")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:4000")
    args = parser.parse_args()
    import os
    create_server(args.root, args.gateway_url, os.getenv("SUBROUTE_API_KEY")).run(transport="stdio")


if __name__ == "__main__":
    main()
