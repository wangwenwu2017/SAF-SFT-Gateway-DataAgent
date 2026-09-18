"""SDK 取消、进度和工厂退出回归。

测试目的：覆盖实际 SDK 队列等待的注销、传输关闭和单 turn 控制。
被测接口：install_queue_wakeup、run_observed_turn、wait_for_factory。
输入与夹具：真实 SDK 路由器、离线通知、独立子进程和临时任务状态。
预期结果：无遗留等待线程；正常完成、超时和取消可区分；提醒一次、worker 有界退出。
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest
from openai_codex import AsyncCodex
from openai_codex.api import AsyncThread
from openai_codex.generated.v2_all import TurnCompletedNotification
from openai_codex.models import Notification

from science_agent_factory.agents.process import wait_for_factory
from science_agent_factory.agents.sdk_control import install_queue_wakeup, run_observed_turn


def test_sdk_waiter_wakes_on_unregister_and_process_exits():
    code = """
import asyncio, threading
from openai_codex import AsyncCodex
from science_agent_factory.agents.sdk_control import install_queue_wakeup
async def main():
    sdk = AsyncCodex(); install_queue_wakeup(sdk)
    router = sdk._client._sync._router
    router.register_turn("test")
    queue = router._turn_notifications["test"]
    entered = threading.Event(); original = queue.get
    def get(*a, **k):
        entered.set(); return original(*a, **k)
    queue.get = get
    task = asyncio.create_task(asyncio.to_thread(router.next_turn_notification, "test"))
    while not entered.is_set(): await asyncio.sleep(.001)
    task.cancel()
    router.unregister_turn("test")
    router.unregister_turn("test")
    router.fail_all(RuntimeError("closed"))
    try: await task
    except asyncio.CancelledError: pass
asyncio.run(main())
print("clean exit")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean exit" in result.stdout


def test_adapter_is_instance_scoped():
    first, second = AsyncCodex(), AsyncCodex()
    install_queue_wakeup(first)
    assert type(first._client._sync._router) is not type(second._client._sync._router)


class Handle:
    id = "turn-test"

    def __init__(self, delay, responsive=True):
        self.delay = delay
        self.responsive = responsive
        self.finished = asyncio.Event()
        self.interrupts = 0
        self.steers = 0

    async def stream(self):
        try:
            await asyncio.wait_for(self.finished.wait(), self.delay)
        except TimeoutError:
            pass
        yield Notification(
            method="turn/completed",
            payload=TurnCompletedNotification(
                threadId="thread-test",
                turn={
                    "id": self.id,
                    "items": [],
                    "status": "interrupted" if self.interrupts else "completed",
                },
            ),
        )

    async def interrupt(self):
        self.interrupts += 1
        if self.responsive:
            self.finished.set()
        else:
            await asyncio.sleep(10)

    async def steer(self, message):
        self.steers += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["complete", "timeout", "cancel", "unresponsive"])
async def test_observed_turn_control(monkeypatch, scenario):
    import science_agent_factory.agents.sdk_control as control

    handle = Handle(0.06 if scenario == "complete" else 10, scenario != "unresponsive")
    closed = []

    async def start(self, message):
        return handle

    async def close(codex, **kwargs):
        closed.append(True)

    monkeypatch.setattr(AsyncThread, "turn", start)
    monkeypatch.setattr(control, "close_sdk", close)
    thread = AsyncThread(SimpleNamespace(), "thread-test")
    events = []
    task = asyncio.create_task(
        run_observed_turn(
            thread,
            "build",
            timeout_s=0.12,
            exploration_s=0.02,
            heartbeat_s=0.01,
            interrupt_grace_s=0.02,
            emit=events.append,
        )
    )
    if scenario == "cancel":
        await asyncio.sleep(0.04)
        task.cancel()
    if scenario == "complete":
        result = await task
        assert result.id == handle.id
        assert handle.interrupts == 0
    else:
        with pytest.raises(asyncio.CancelledError if scenario == "cancel" else TimeoutError):
            await task
        assert handle.interrupts == 1
    assert handle.steers == 1
    assert sum(e["type"] == "exploration_budget_reminder" for e in events) == 1
    assert any(e["type"] == "heartbeat" for e in events)
    assert bool(closed) == (scenario == "unresponsive")


def test_terminal_worker_is_killed_and_log_forwarded(tmp_path):
    state = tmp_path / "jobs/job/state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"job_id": "job", "status": "failed"}))
    log = tmp_path / "factory.log"
    log.write_text("timeout evidence\n")
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert worker.stdout.readline().strip() == "ready"
    messages = []
    try:
        code = wait_for_factory(
            worker,
            tmp_path,
            log,
            terminal_grace_s=0.03,
            term_grace_s=0.03,
            poll_s=0.01,
            emit=messages.append,
        )
        assert code == -signal.SIGKILL
        assert "timeout evidence" in messages
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait()
