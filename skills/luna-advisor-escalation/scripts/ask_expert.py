#!/usr/bin/env python3
"""Ask the dedicated local expert API for a compact, advice-only review."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
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


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive.")

    packet = read_packet(args)
    payload = {
        "model": MODEL_ALIASES[args.model],
        "stream": False,
        "messages": [
            {"role": "developer", "content": ADVISOR_INSTRUCTION},
            {"role": "user", "content": packet},
        ],
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
        print(f"Expert API returned HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError) as exc:
        print(f"Expert API is unavailable: {exc}", file=sys.stderr)
        return 1

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print("Expert API returned no usable advisor content.", file=sys.stderr)
        return 1
    if not isinstance(content, str) or not content.strip():
        print("Expert API returned empty advisor content.", file=sys.stderr)
        return 1

    result = {
        "model": body.get("model", MODEL_ALIASES[args.model]),
        "advice": content.strip(),
        "packet_chars": len(packet),
        "advice_words": len(content.split()),
        "usage": body.get("usage"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
