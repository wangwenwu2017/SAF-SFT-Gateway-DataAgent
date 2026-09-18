"""工作流状态的通用原子 JSON 持久化。

模块用途：统一 Job 和固定槽位 Batch 的单写者锁、模型校验、fsync 和原子替换。
主要接口：``AtomicJsonStore``、``JobFileLock``。
输入：状态文件路径、Pydantic 模型类型和状态对象。
输出：经过校验的状态对象以及持久化 JSON 文件。
副作用与边界：创建目录和临时文件并调用 ``os.replace``；只保证单机文件系统上的单写者语义。
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Generic, Self, TypeVar

from pydantic import BaseModel

StateT = TypeVar("StateT", bound=BaseModel)


class JobFileLock:
    """基于 flock 的非阻塞单写者锁。"""

    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.close()
            self._stream = None
            raise RuntimeError(f"job is already running: {self.path.parent.name}") from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class AtomicJsonStore(Generic[StateT]):
    """对一个 Pydantic 状态模型执行原子读写。"""

    def __init__(self, root: Path, filename: str, model: type[StateT]):
        self.root = root.resolve()
        self.path = self.root / filename
        self.lock_path = self.root / f".{filename.removesuffix('.json')}.lock"
        self.model = model

    def lock(self) -> JobFileLock:
        return JobFileLock(self.lock_path)

    def create(self, state: StateT) -> StateT:
        if self.path.exists():
            raise FileExistsError(f"state already exists: {self.path}")
        self.save(state)
        return state

    def load(self) -> StateT:
        return self.model.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: StateT) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if hasattr(state, "updated_at"):
            state.updated_at = datetime.now(UTC)
        payload = self.model.model_validate(state.model_dump(mode="json"))
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


__all__ = ["AtomicJsonStore", "JobFileLock"]
