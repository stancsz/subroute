"""Bounded Claude Code execution, with process-tree cleanup on every exit."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import shutil
import signal

_CLI_RECEIPT_MAX_BYTES = 16000


def _failure_receipt(error_type, session_id=None):
    """Return a small valid failure receipt, retaining a usable session when it fits."""
    receipt = {"error_type": error_type}
    if session_id is not None:
        candidate = {**receipt, "session_id": session_id}
        try:
            encoded = json.dumps(candidate, ensure_ascii=False)
            if len(encoded.encode("utf-8")) <= _CLI_RECEIPT_MAX_BYTES:
                return encoded
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            pass
    return json.dumps(receipt)


def _encode_receipt(metadata, result_text, result_limit):
    """Truncate only result text so the complete UTF-8 JSON receipt fits its contract."""
    text = result_text[:result_limit]

    def encode(candidate):
        serialized = json.dumps({**metadata, "result": candidate}, ensure_ascii=False)
        return serialized, len(serialized.encode("utf-8"))

    try:
        empty, empty_size = encode("")
        if empty_size > _CLI_RECEIPT_MAX_BYTES:
            return None, "metadata_too_large"
        full, full_size = encode(text)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return None, "metadata_invalid"

    if full_size <= _CLI_RECEIPT_MAX_BYTES:
        return full, None

    # UTF-8 JSON size is monotonic as characters are appended, so find the longest
    # valid prefix in logarithmic serializations rather than repeatedly shrinking it.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        try:
            _, size = encode(text[:middle])
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            return None, "metadata_invalid"
        if size <= _CLI_RECEIPT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1

    if low == 0 and text:
        return None, "metadata_too_large"
    return encode(text[:low])[0], None


def claude_command():
    command = shutil.which("claude")
    if not command:
        raise RuntimeError("Install Claude Code on this machine first")
    if os.name == "nt" and Path(command).suffix.lower() in {".cmd", ".ps1", ".bat"}:
        package = Path(command).parent / "node_modules/@anthropic-ai/claude-code"
        metadata = package / "package.json"
        bins = json.loads(metadata.read_text(encoding="utf-8"))["bin"] if metadata.is_file() else {}
        entry = bins.get("claude", "") if isinstance(bins, dict) else bins
        script = (package / entry).resolve()
        node = shutil.which("node")
        if not entry or not script.is_relative_to(package.resolve()) or not script.is_file():
            raise RuntimeError("Use Claude's native executable or an installed npm Claude Code CLI")
        if script.suffix.lower() == ".exe":
            return [str(script)]
        if not node:
            raise RuntimeError("The installed JavaScript Claude CLI requires Node.js")
        return [node, str(script)]
    return [command]


class WindowsJob:
    """Kill descendants if the owning runner dies. Windows assigns immediately after spawn."""
    def __init__(self, pid):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION has architecture-dependent pointer fields.
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                       ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                       ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if (not process or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                    or not kernel.AssignProcessToJobObject(self.handle, process)):
                raise OSError(ctypes.get_last_error(), "Cannot contain Claude process tree")
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


async def stop_tree(process, job=None):
    if job:
        job.close()
    elif os.name == "nt":
        # Also used if Job assignment failed. Arguments never include task text.
        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def terminal_result(events: list[dict], returncode: int):
    results = [event for event in events if event.get("type") == "result"]
    if len(results) != 1:
        return "failed", "Claude did not provide exactly one terminal JSON result"
    final = results[0]
    text = str(final.get("result", ""))
    session_id = final.get("session_id")
    if returncode != 0 or final.get("is_error") is not False or final.get("subtype") != "success":
        error_type = final.get("subtype", "unknown")
        metadata = {"error_type": error_type, "session_id": session_id,
                    "usage": final.get("usage"), "total_cost_usd": final.get("total_cost_usd"),
                    "num_turns": final.get("num_turns")}
        receipt, error = _encode_receipt(metadata, text, 12000)
        if error:
            return "failed", _failure_receipt(error, session_id)
        return "failed", receipt
    if not text.strip():
        return "failed", "Claude reported success without result evidence"
    metadata = {"session_id": session_id, "usage": final.get("usage"),
                "total_cost_usd": final.get("total_cost_usd"), "num_turns": final.get("num_turns")}
    receipt, error = _encode_receipt(metadata, text, 13000)
    if error:
        return "failed", _failure_receipt(error, session_id)
    return "success", receipt


async def execute(command, prompt, cwd, env, timeout, pulse=None, pulse_interval=10):
    process = None
    job = None
    events = []
    readers = []
    activity = "starting"
    cleaned = False
    outcome, result = "failed", "Execution did not start"

    async def stdout():
        nonlocal activity
        size = 0
        async for line in process.stdout:
            size += len(line)
            if size > 8_000_000:
                raise RuntimeError("Claude output exceeded 8 MB run limit")
            event = json.loads(line)
            if not isinstance(event, dict):
                raise RuntimeError("Claude emitted invalid JSON event")
            if event.get("type") == "result":
                events.append(event)
            if event.get("type") == "assistant":
                for part in event.get("message", {}).get("content", []):
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        activity = "tool:" + str(part.get("name", "unknown"))[:80]

    async def stderr():
        # Drain without retaining possibly credential-bearing diagnostic output.
        async for _chunk in process.stderr:
            pass

    async def monitor():
        if pulse:
            while True:
                await asyncio.sleep(pulse_interval)
                if await pulse(activity):
                    raise InterruptedError("Cancellation requested by coordinator")
        else:
            await asyncio.Future()

    try:
        process = await asyncio.create_subprocess_exec(*command, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt", limit=1_000_000)
        if os.name == "nt":
            job = WindowsJob(process.pid)
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        readers = [asyncio.create_task(stdout()), asyncio.create_task(stderr())]
        async def wait_exit():
            # asyncio Process.wait can wait on inherited descendant pipes after
            # the direct child has exited. Observe its returncode, then kill the group.
            while process.returncode is None:
                await asyncio.sleep(0.05)
        waiter = asyncio.create_task(wait_exit())
        watcher = asyncio.create_task(monitor())
        readers.extend([waiter, watcher])
        async with asyncio.timeout(timeout):
            # A pipe parsing error or heartbeat failure must interrupt execution promptly.
            while not waiter.done():
                done, _pending = await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
                for item in done:
                    if item.exception():
                        raise item.exception()
                if waiter.done():
                    break
                # Completed stream readers would otherwise cause a busy loop.
                readers = [item for item in readers if not item.done()]
            # Descendants must not keep stdout open after Claude exits.
            await stop_tree(process, job)
            cleaned = True
            job = None
            for reader in readers:
                if reader is not watcher and reader is not waiter:
                    await reader
        outcome, result = terminal_result(events, process.returncode)
    except InterruptedError:
        outcome, result = "cancelled", "Coordinator requested cancellation; process tree stopped"
    except TimeoutError:
        outcome, result = "failed", "Execution exceeded its wall-clock deadline; process tree stopped"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Do not echo SDK errors/headers, executable arguments, credentials or task text.
        outcome, result = "failed", f"Execution stopped ({type(exc).__name__}); inspect local configuration"
    finally:
        if process and not cleaned:
            await stop_tree(process, job)
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
    return outcome, result


def cli_args(model, turns, budget, tools, mcp_config=None):
    # --tools selects built-in names; --allowedTools carries permission patterns.
    builtins = list(dict.fromkeys(tool.split("(", 1)[0] for tool in tools))
    return claude_command() + ["-p", "--output-format", "stream-json", "--verbose", "--model", model,
        "--max-turns", str(turns), "--max-budget-usd", str(budget), "--permission-mode", "dontAsk",
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", mcp_config or '{"mcpServers":{}}',
        "--tools", ",".join(builtins), "--allowedTools", ",".join(tools) if mcp_config is None else "mcp__relay__*"]
