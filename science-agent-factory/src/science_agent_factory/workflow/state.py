"""正式工作流的原子 JSON 状态存储与单进程文件锁。

模块用途：用一个权威 ``state.json`` 替代 SQLite 命令队列、租约、心跳和事件归约器。
主要接口：``JobStateStore`` 的 ``create/load/save``，以及 ``JobFileLock`` 上下文管理器。
输入：Job 根目录和 ``JobState``。
输出：经过模型校验的状态对象和原子替换后的 JSON 文件。
副作用与边界：创建目录、锁文件和临时文件并调用 ``os.replace``；只保证单机文件系统上的单写者。
"""

from __future__ import annotations

from pathlib import Path

from ..contracts.state import JobState
from .persistence import AtomicJsonStore, JobFileLock


class JobStateStore(AtomicJsonStore[JobState]):
    """JobState 的类型化状态存储。"""

    def __init__(self, job_dir: Path):
        super().__init__(job_dir, "state.json", JobState)
        self.job_dir = self.root
        self.lock_path = self.root / ".job.lock"


__all__ = ["AtomicJsonStore", "JobFileLock", "JobStateStore"]
