"""Bounded, synthetic llama.cpp concurrency probe. Does not assess model quality."""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import http.client
import json
import math
from pathlib import Path
import random
import socket
import threading
import time
from urllib.parse import urlsplit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--input-tokens", type=int, default=32768)
    parser.add_argument("--output-tokens", type=int, default=512)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--min-aggregate-tps", type=float, default=30,
                        help="minimum aggregate wall-clock decode tokens/s for acceptance (default: 30)")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--progress-jsonl", type=Path,
                        help="optional append-only token-count evidence; must be a new file")
    args = parser.parse_args()
    if not (1 <= args.input_tokens <= 262144 and 2 <= args.output_tokens <= 4096):
        parser.error("input tokens must be 1..262144; output tokens must be 2..4096")
    if not (1 <= args.parallel <= 4 and 0 < args.timeout_seconds <= 3600):
        parser.error("parallel must be 1..4; timeout seconds must be greater than 0 and at most 3600")
    if not math.isfinite(args.min_aggregate_tps) or args.min_aggregate_tps <= 0:
        parser.error("minimum aggregate tokens/s must be finite and positive")
    url = urlsplit(args.base_url)
    if url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment:
        parser.error("base URL must be an HTTP(S) URL without a query or fragment")
    if args.progress_jsonl:
        args.progress_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with args.progress_jsonl.open("x", encoding="utf-8"):
            pass

    started_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    live_sockets: set[socket.socket] = set()
    lock = threading.Lock()
    cancelled = threading.Event()

    def progress(kind: str, **values) -> None:
        if args.progress_jsonl:
            with lock:
                row = {"kind": kind, "utc": datetime.now(timezone.utc).isoformat(),
                       "elapsed_seconds": time.monotonic() - started, **values}
                with args.progress_jsonl.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")

    progress("benchmark_started", parallel=args.parallel,
             input_tokens_per_request=args.input_tokens,
             output_tokens_per_request=args.output_tokens)

    def cancel() -> None:
        cancelled.set()
        with lock:
            sockets = list(live_sockets)
        for connection_socket in sockets:
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(args.timeout_seconds, cancel)
    watchdog.daemon = True
    watchdog.start()

    def post(path: str, body: dict) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, socket.socket]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled.is_set():
            raise TimeoutError("benchmark deadline exceeded")
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(url.hostname, url.port, timeout=remaining)
        connection_socket = None
        try:
            connection.connect()
            connection_socket = connection.sock
            assert connection_socket is not None
            with lock:
                if cancelled.is_set():
                    raise TimeoutError("benchmark cancelled")
                live_sockets.add(connection_socket)
            connection.request("POST", url.path.rstrip("/") + path, json.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"{path} returned HTTP {response.status}")
            return connection, response, connection_socket
        except BaseException:
            connection.close()
            if connection_socket is not None:
                with lock:
                    live_sockets.discard(connection_socket)
            raise

    def close(connection, response, connection_socket) -> None:
        response.close()
        connection.close()
        with lock:
            live_sockets.discard(connection_socket)

    def tokenize(text: str) -> list[int]:
        connection, response, connection_socket = post("/tokenize", {"content": text, "add_special": False})
        try:
            tokens = json.load(response)["tokens"]
            if not isinstance(tokens, list) or not all(isinstance(token, int) for token in tokens):
                raise RuntimeError("tokenizer did not return integer token IDs")
            return tokens
        finally:
            close(connection, response, connection_socket)

    def prompt(slot: int) -> list[int]:
        rng = random.Random(81273 + slot)
        # Distinct first lines and records avoid cross-request prefix reuse.
        records = [f"Independent request {slot}: analyze the following synthetic audit records.\n"]
        for index in range(max(32, args.input_tokens // 20 + 64)):
            records.append(f"request={slot} item={index} value={rng.getrandbits(64):016x} status=recorded\n")
        text = "".join(records)
        tokens = tokenize(text)
        if len(tokens) < args.input_tokens:
            text += text * math.ceil((args.input_tokens - len(tokens)) / max(1, len(tokens)))
            tokens = tokenize(text)
        if len(tokens) < args.input_tokens:
            raise RuntimeError("could not create the requested prompt token count")
        return tokens[:args.input_tokens]

    barrier = threading.Barrier(args.parallel)

    def completion(slot: int, tokens: list[int]) -> dict:
        result = {"slot": slot, "input_tokens": len(tokens), "events": []}
        connection = response = connection_socket = None
        try:
            barrier.wait(timeout=max(0.001, deadline - time.monotonic()))
            result["request_started_seconds"] = time.monotonic() - started
            progress("request_started", slot=slot)
            connection, response, connection_socket = post("/completion", {
                "prompt": tokens, "id_slot": slot, "n_predict": args.output_tokens,
                "stream": True, "return_tokens": True, "ignore_eos": True,
                "cache_prompt": False, "temperature": 0, "seed": 81273 + slot,
            })
            terminal = None
            for raw_line in response:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("benchmark deadline exceeded")
                if not raw_line.startswith(b"data:"):
                    continue
                payload = raw_line[5:].strip()
                if payload == b"[DONE]":
                    break
                event = json.loads(payload)
                if "error" in event:
                    raise RuntimeError("server reported an inference error")
                if event.get("stop") is True:
                    terminal = event
                    break
                event_tokens = event.get("tokens", [])
                if not isinstance(event_tokens, list):
                    raise RuntimeError("stream token IDs were not a list")
                if event_tokens:
                    result["events"].append([time.monotonic() - started, len(event_tokens)])
                    progress("token", slot=slot, tokens=len(event_tokens))
            if terminal is None:
                raise RuntimeError("stream ended without a terminal completion")
            generated = terminal.get("tokens_predicted", terminal.get("timings", {}).get("predicted_n"))
            streamed = sum(count for _, count in result["events"])
            result.update({
                "generated_tokens": generated, "streamed_tokens": streamed,
                "server_prompt_tokens": terminal.get("tokens_evaluated"),
                # b11610: timings.cache_n is prompt reuse; tokens_cached is
                # resident state at completion, including generated tokens.
                "server_prompt_cached_tokens": terminal.get("timings", {}).get("cache_n"),
                "server_cached_tokens_at_end": terminal.get("tokens_cached"),
                "stop_type": terminal.get("stop_type"), "truncated": terminal.get("truncated"),
                "server_timings": terminal.get("timings"),
                "completed_seconds": time.monotonic() - started,
            })
            if not result["events"] or generated != streamed or generated < args.output_tokens:
                raise RuntimeError("generated/streamed token counts do not establish the requested decode")
            if terminal.get("truncated") is True:
                raise RuntimeError("server truncated the prompt/context")
            result["time_to_first_token_seconds"] = result["events"][0][0] - result["request_started_seconds"]
            result["ok"] = True
            progress("terminal", slot=slot, generated_tokens=generated)
        except Exception as error:
            result.update(ok=False, error=f"{type(error).__name__}: {error}")
            cancel()
            progress("request_error", slot=slot, error=result["error"])
        finally:
            if connection is not None:
                close(connection, response, connection_socket)
        return result

    report = {"base_url": args.base_url, "parallel": args.parallel,
              "started_at_utc": started_at_utc,
              "input_tokens_per_request": args.input_tokens,
              "output_tokens_requested_per_request": args.output_tokens,
              "timeout_seconds": args.timeout_seconds,
              "minimum_aggregate_decode_tokens_per_second": args.min_aggregate_tps,
              "acceptance_scope": "synthetic uncached concurrent decode; excludes gaming, VRAM and quality",
              "synthetic_probe_not_quality_assessment": True}
    try:
        prompts = [prompt(slot) for slot in range(args.parallel)]
        report["prompt_preparation_seconds"] = time.monotonic() - started
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
            results = list(executor.map(lambda pair: completion(*pair), enumerate(prompts)))
        report["requests"] = results
        report["ok"] = all(result["ok"] for result in results)
        if report["ok"]:
            first = min(result["events"][0][0] for result in results)
            last = max(result["events"][-1][0] for result in results)
            overlap_first = max(result["events"][0][0] for result in results)
            overlap_last = min(result["events"][-1][0] for result in results)
            events = [event for result in results for event in result["events"]]
            total = sum(result["generated_tokens"] for result in results)
            report.update({
                "aggregate_decode_tokens_per_second": sum(count for at, count in events if at > first) / (last - first),
                "aggregate_end_to_end_tokens_per_second": total / (
                    max(result["completed_seconds"] for result in results)
                    - min(result["request_started_seconds"] for result in results)),
                "all_requests_overlap_seconds": max(0, overlap_last - overlap_first),
                "all_requests_overlap_decode_tokens_per_second": (
                    sum(count for at, count in events if overlap_first < at <= overlap_last)
                    / (overlap_last - overlap_first) if overlap_last > overlap_first else None),
            })
    except Exception as error:
        cancel()
        report.update(ok=False, error=f"{type(error).__name__}: {error}")
    finally:
        watchdog.cancel()
        cancel()
    results = report.get("requests", [])
    complete_count = len(results) == args.parallel
    report["acceptance_checks"] = {
        "all_requests_completed": complete_count and report["ok"],
        "server_prompt_counts_verified": complete_count and all(
            type(result.get("server_prompt_tokens")) is int
            and result["server_prompt_tokens"] >= args.input_tokens for result in results),
        "no_prompt_cache_reuse": complete_count and all(
            type(result.get("server_prompt_cached_tokens")) is int
            and result["server_prompt_cached_tokens"] == 0 for result in results),
        "positive_all_requests_decode_overlap": report.get("all_requests_overlap_seconds", 0) > 0,
        "aggregate_decode_rate_met": report.get("aggregate_decode_tokens_per_second", 0) >= args.min_aggregate_tps,
    }
    report["acceptance_passed"] = all(report["acceptance_checks"].values())
    report["elapsed_seconds"] = time.monotonic() - started
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "requests"}
    print(json.dumps(summary, indent=2))
    return 0 if report["acceptance_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
