#!/usr/bin/env python3
"""Ask the dedicated local expert API for a compact, advice-only review."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_PACKET_CHARS = 6_000
MODEL_ALIASES = {
    "sol": "codex-sol-advisor",
    "astra": "codex-astra-advisor",
}
ADVISOR_INSTRUCTION = """You are a compact decision advisor, not the task executor.
Every token must change the worker's next decision. Do not repeat the packet, explain
obvious background, write code, use tools, modify files, contact services, or claim
verification. Return exactly these three lines, with no preamble or markdown:
Verdict: one decisive sentence.
Next: at most three short, testable actions.
Risk: one material risk or stop condition, or `none`.
Target 160 words or fewer. Do not list alternatives unless the packet asks for them."""


def configure_utf8_stdout(stdout: TextIO | None = None) -> None:
    """Make JSON advice readable on Windows consoles using legacy code pages."""
    stream = sys.stdout if stdout is None else stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODEL_ALIASES, default="sol")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-file", type=Path)
    source.add_argument("--question")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EXPERTS_BASE_URL", "http://127.0.0.1:4040/v1"),
        help="Expert API base URL. Defaults to the local loopback service.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=75.0)
    parser.add_argument("--reader-root", type=Path,
                        help="Enable expert-directed Pi reading within this Git repository.")
    parser.add_argument("--reader-scope", action="append", default=[],
                        help="Approved relative source directory/file; repeat as needed.")
    parser.add_argument("--reader-model", default="current",
                        help="Alias requested from localhost:4000; gateway policy still applies.")
    return parser.parse_args()


def read_packet(args: argparse.Namespace) -> str:
    packet = args.question
    if args.input_file is not None:
        try:
            packet = args.input_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"Unable to read advisor packet: {exc}") from exc
    assert packet is not None
    packet = packet.strip()
    if not packet:
        raise SystemExit("Advisor packet must not be empty.")
    if len(packet) > MAX_PACKET_CHARS:
        raise SystemExit(
            f"Advisor packet is {len(packet)} characters; limit is {MAX_PACKET_CHARS}. "
            "Compact the evidence before consulting."
        )
    return packet


def consult(args: argparse.Namespace, messages: list[dict]) -> dict:
    started = time.monotonic()
    payload = {
        "model": MODEL_ALIASES[args.model],
        "stream": False,
        "messages": messages,
    }
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("EXPERTS_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=args.timeout_seconds) as response:
            body = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1_000]
        raise RuntimeError(f"Expert API returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Expert API is unavailable: {exc}") from exc

    try:
        choice = body["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Expert API returned no usable advisor content.") from exc
    if choice.get("finish_reason") != "stop":
        raise RuntimeError("Expert API did not return a completed answer.")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Expert API returned empty advisor content.")

    result = {
        "model": body.get("model", MODEL_ALIASES[args.model]),
        "advice": content.strip(),
        "advice_words": len(content.split()),
        "usage": body.get("usage"),
        "request_id": body.get("id"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    return result


def main() -> int:
    configure_utf8_stdout()
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive.")
    if args.reader_scope and not args.reader_root:
        raise SystemExit("--reader-scope requires --reader-root.")
    packet = read_packet(args)
    try:
        if args.reader_root:
            from expert_reader import run_with_reader
            result = run_with_reader(args, packet, consult, ADVISOR_INSTRUCTION)
        else:
            result = consult(args, [
                {"role": "developer", "content": ADVISOR_INSTRUCTION},
                {"role": "user", "content": packet},
            ])
        result["packet_chars"] = len(packet)
    except (RuntimeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status", "ok") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
