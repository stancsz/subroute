"""The narrow audio input contract shared by the gateway and its MCP client."""

from __future__ import annotations

import base64
import binascii
import io
import wave
from typing import Any

MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_FILES = 2
AUDIO_REQUEST_BYTES = 29 * 1024 * 1024


def decode_audio(audio: dict[str, Any]) -> bytes:
    if not isinstance(audio, dict) or set(audio) != {"data", "format"}:
        raise ValueError("input_audio requires exactly data and format")
    if not isinstance(audio["format"], str) or audio["format"] not in {"wav", "mp3"}:
        raise ValueError("audio supports PCM WAV and MP3 only")
    data = audio["data"]
    if not isinstance(data, str) or not data or len(data) > (MAX_AUDIO_BYTES + 2) // 3 * 4:
        raise ValueError("audio must be non-empty base64, at most 20 MiB decoded")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid audio base64") from exc
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        raise ValueError("audio must be non-empty, at most 20 MiB decoded")
    if audio["format"] == "wav":
        try:
            with wave.open(io.BytesIO(raw), "rb") as wav:
                if wav.getnchannels() not in {1, 2} or wav.getsampwidth() not in {1, 2, 3, 4}:
                    raise ValueError("WAV must be mono/stereo PCM")
                if not 8000 <= wav.getframerate() <= 192000 or not wav.getnframes():
                    raise ValueError("invalid WAV sample rate or empty WAV")
                frames = wav.readframes(wav.getnframes())
                if len(frames) != wav.getnframes() * wav.getnchannels() * wav.getsampwidth():
                    raise ValueError("truncated WAV")
        except (wave.Error, EOFError) as exc:
            raise ValueError("invalid or unsupported PCM WAV") from exc
    elif not (raw.startswith(b"ID3") or (len(raw) > 1 and raw[0] == 255 and raw[1] & 224 == 224)):
        raise ValueError("invalid MP3 header")
    return raw


def _contains_audio(value) -> bool:
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and kind in {"input_audio", "audio"}:
            return True
        return any(_contains_audio(item) for item in value.values())
    return isinstance(value, list) and any(_contains_audio(item) for item in value)


def audio_blocks(messages: list) -> list[dict]:
    """Only explicit top-level user audio is supported; never infer it from text."""
    found = []

    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("type"), str) and block.get("type") in {"input_audio", "audio"}:
                if message.get("role") != "user":
                    raise ValueError("audio is supported only in user messages")
                found.append(block)
            elif _contains_audio(block):
                raise ValueError("nested audio is unsupported; use top-level user input_audio")
    return found


def audio_route(data: dict, call_type: str) -> bool:
    if call_type in {"responses", "aresponses"}:
        # Responses has its own input envelope. Never let an audio block
        # disappear inside a later LiteLLM conversion to Chat.
        if _contains_audio(data.get("input")):
            raise ValueError("Audio requires /v1/chat/completions input_audio; Responses audio is unsupported")
    blocks = audio_blocks(data.get("messages") or [])
    if not blocks:
        return False
    if call_type not in {"completion", "acompletion"}:
        # LiteLLM 1.103.0 drops unrecognised Anthropic audio blocks. Reject
        # before its conversion instead of reporting success without audio.
        raise ValueError("Audio requires /v1/chat/completions input_audio; Messages/Responses audio is unsupported")
    if len(blocks) > MAX_AUDIO_FILES:
        raise ValueError("audio supports at most two attachments")
    total = 0
    for block in blocks:
        if set(block) != {"type", "input_audio"} or block.get("type") != "input_audio":
            raise ValueError("Use OpenAI input_audio blocks for audio")
        total += len(decode_audio(block["input_audio"]))
    if total > MAX_AUDIO_BYTES:
        raise ValueError("audio attachments exceed 20 MiB combined")
    return True
