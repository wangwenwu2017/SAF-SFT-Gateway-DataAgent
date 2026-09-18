"""独立进程组的有界终止。

模块用途：为 Harbor rollout 和环境预热提供取消清理，并监控全链路工厂 worker 的终态退出。
主要接口：terminate_process_group、wait_for_factory。
输入：独立进程组的子进程、宽限时间，以及工厂状态和日志路径。
输出：终止结果或 worker 退出码，并转发日志与状态进度。
副作用与边界：仅向传入进程的独立进程组发信号；不管理容器或其他进程组。
"""

import asyncio
import contextlib
import os
import signal


async def terminate_process_group(
    process: asyncio.subprocess.Process, *, grace_s: float = 30
) -> None:
    """主进程退出后仍清理可能忽略 TERM 的后代。"""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
    except TimeoutError:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def wait_for_factory(
    process, storage_root, log_path, *, terminal_grace_s=30, term_grace_s=10, poll_s=1, emit=print
):
    """监控专属同步 worker；终态后退出挂起时终止其进程组，持续转发日志。"""
    import json
    import time

    terminal_since = None
    offset = 0
    previous = None
    while True:
        code = process.poll()
        if log_path.exists():
            with log_path.open() as log:
                log.seek(offset)
                for line in log:
                    emit(line.rstrip())
                offset = log.tell()
        if code is not None:
            return code
        states = []
        for path in (storage_root / "jobs").glob("*/state.json"):
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            states.append(state)
        current = [(s["job_id"], s["status"], s.get("active_step")) for s in states]
        if current != previous:
            emit(f"Factory progress: {current}")
            previous = current
        terminal = bool(states) and all(s["status"] in {"curated", "failed"} for s in states)
        if terminal:
            terminal_since = terminal_since or time.monotonic()
            if time.monotonic() - terminal_since >= terminal_grace_s:
                emit(
                    "Factory reached terminal state but did not exit; terminating its process group"
                )
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                import subprocess

                try:
                    process.wait(timeout=term_grace_s)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                return process.wait(timeout=term_grace_s)
        else:
            terminal_since = None
        time.sleep(poll_s)
