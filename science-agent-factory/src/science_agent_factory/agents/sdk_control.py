"""固定版本 SDK 的取消适配与可观测 turn 执行。

模块用途：修复通知注销后的阻塞等待，并在单个 turn 内提供心跳、软预算提醒和有界中断。
主要接口：install_queue_wakeup、close_sdk、run_observed_turn。
输入：本机 openai-codex 0.147.0 客户端、线程、消息、时间预算和事件回调。
输出：原生 TurnResult、实时进度事件或原始失败异常。
副作用与边界：仅替换当前客户端尚未启动的路由器；调用 SDK 中断/关闭，不修改依赖文件或全局类。
不执行工作流状态转换，不放宽任务交付条件，不输出原始推理或认证数据。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from importlib.metadata import version

from openai_codex import AsyncCodex
from openai_codex._message_router import MessageRouter
from openai_codex._run import _collect_async_turn_result
from openai_codex.api import AsyncThread


class _WakeableRouter(MessageRouter):
    def unregister_turn(self, turn_id: str) -> None:
        with self._lock:
            waiter = self._turn_notifications.pop(turn_id, None)
            self._pending_turn_notifications.pop(turn_id, None)
        if waiter is not None:
            waiter.put(RuntimeError(f"turn {turn_id!r} stream closed"))


def install_queue_wakeup(codex) -> None:
    """在启动真实 SDK 客户端前安装实例级适配；注入的测试运行时不涉及 SDK 路由。"""
    if not isinstance(codex, AsyncCodex):
        return
    if version("openai-codex") != "0.147.0":
        raise RuntimeError("SDK cancellation adapter requires openai-codex==0.147.0")
    client = codex._client._sync
    if type(client._router) is not MessageRouter or client._proc is not None:
        raise RuntimeError("SDK cancellation adapter requires a fresh compatible client")
    client._router = _WakeableRouter()


async def close_sdk(codex, *, timeout_s: float = 10) -> None:
    """有界关闭；必要时唤醒所有路由等待者并结束本客户端子进程。"""
    client = codex._client._sync if isinstance(codex, AsyncCodex) else None
    proc = client._proc if client is not None else None
    closing = asyncio.create_task(codex.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(asyncio.shield(closing), timeout_s)
    except (TimeoutError, asyncio.CancelledError):
        if client is not None:
            client._router.fail_all(RuntimeError("SDK shutdown deadline exceeded"))
        if proc is not None and proc.poll() is None:
            proc.kill()
        closing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(closing, 1)
        raise


async def run_observed_turn(
    thread,
    message,
    *,
    timeout_s,
    exploration_s,
    emit,
    heartbeat_s=30,
    interrupt_grace_s=10,
):
    """执行并观测原生 turn；软预算仅 steer 一次，不创建独立设计调用。"""
    if not isinstance(thread, AsyncThread):
        # AgentRuntime 测试替身保留文件交付协议，不伪装成原生 SDK 流。
        async with asyncio.timeout(timeout_s):
            return await thread.run(message)
    started = time.monotonic()
    handle = None
    collector = None
    monitor = None
    latest = {"activity": "turn/start", "tokens": None}

    async def events():
        stream = handle.stream()
        try:
            async for event in stream:
                if event.method in {
                    "turn/started",
                    "turn/completed",
                    "item/started",
                    "item/completed",
                    "thread/tokenUsage/updated",
                }:
                    payload = event.payload.model_dump(mode="json", by_alias=True)
                    item = payload.get("item", {})
                    latest["activity"] = event.method + (
                        ":" + item["type"] if "type" in item else ""
                    )
                    usage = payload.get("tokenUsage")
                    if usage is not None:
                        latest["tokens"] = usage.get("total")
                    emit(
                        {
                            "type": "sdk_progress",
                            "method": event.method,
                            "turn_id": handle.id,
                            "item_type": item.get("type"),
                            "item_id": item.get("id"),
                            "usage": usage,
                        }
                    )
                yield event
        finally:
            await stream.aclose()

    async def heartbeat():
        reminded = False
        while True:
            await asyncio.sleep(heartbeat_s)
            elapsed = time.monotonic() - started
            emit(
                {
                    "type": "heartbeat",
                    "elapsed_s": round(elapsed),
                    "remaining_s": max(0, round(timeout_s - elapsed)),
                    **latest,
                }
            )
            if exploration_s and not reminded and elapsed >= exploration_s:
                reminded = True
                emit({"type": "exploration_budget_reminder", "elapsed_s": round(elapsed)})
                try:
                    await asyncio.wait_for(
                        handle.steer(
                            "探索软预算已到。若仍在研究 Seed，请依据已有参考解和证据收敛方案，"
                            "立即落盘设计卡并进入实现；不要继续追求完整反推隐藏生成机制。"
                            f"本轮约剩 {max(0, int(timeout_s - elapsed))} 秒，请为真实 Oracle/Nop 自测"
                            "及完整交付保留时间。若已在实现或自测，请继续；不能降低难度、放宽测试或伪造成功。"
                        ),
                        5,
                    )
                except Exception as exc:  # noqa: BLE001 - 提醒失败不能遮蔽主 turn
                    emit({"type": "steer_failed", "error_type": type(exc).__name__})

    try:
        async with asyncio.timeout(timeout_s):
            handle = await thread.turn(message)
            emit({"type": "turn_started", "thread_id": thread.id, "turn_id": handle.id})
            collector = asyncio.create_task(_collect_async_turn_result(events(), turn_id=handle.id))
            monitor = asyncio.create_task(heartbeat())
            result = await asyncio.shield(collector)
            if str(result.status) not in {"completed", "TurnStatus.completed"}:
                raise RuntimeError(f"Codex turn ended with status={result.status}")
            return result
    except (TimeoutError, asyncio.CancelledError):
        if handle is not None and collector is not None and not collector.done():
            try:
                async with asyncio.timeout(interrupt_grace_s):
                    await handle.interrupt()
                    await asyncio.shield(collector)
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - 保留原始取消或超时
                emit({"type": "interrupt_incomplete", "error_type": type(exc).__name__})
                # Close transport before cancelling the stream, so active response waiters wake too.
                with contextlib.suppress(Exception):
                    await close_sdk(thread._codex, timeout_s=interrupt_grace_s)
        raise
    finally:
        for task in (monitor, collector):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
