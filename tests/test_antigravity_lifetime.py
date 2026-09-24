import asyncio

import pytest

from subroute.handlers import antigravity


@pytest.mark.parametrize("cancel", [False, True])
def test_abort_kills_and_reaps_cli(monkeypatch, cancel):
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        class Process:
            returncode = None
            reaped = False

            async def communicate(self, data):
                started.set()
                await stopped.wait()
                return b"", b""

            def kill(self):
                self.returncode = -9
                stopped.set()

            async def wait(self):
                await stopped.wait()
                self.reaped = True

        process = Process()

        async def spawn(*args, **kwargs):
            return process

        monkeypatch.setenv("AGY_PATH", "fixture-agy")
        monkeypatch.setattr(antigravity.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(antigravity.invoke_agy("gemini-3.8-flash", "hello", timeout=0.02))
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
            await task
        assert process.returncode == -9
        assert process.reaped

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_real_subprocess_is_reaped(monkeypatch, cancel):
    import sys

    async def scenario():
        spawned = asyncio.Event()
        children = []
        original_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await original_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
            children.append(process)
            spawned.set()
            return process

        monkeypatch.setenv("AGY_PATH", "fixture-agy")
        monkeypatch.setattr(antigravity.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(antigravity.invoke_agy("gemini-3.8-flash", "hello", timeout=0.1))
        await spawned.wait()
        if cancel:
            await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
            await task
        assert children[0].returncode is not None

    asyncio.run(scenario())
