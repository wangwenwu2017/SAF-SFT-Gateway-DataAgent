"""Job 与 Batch 编排、状态和工作区的公共入口。

模块用途：以延迟导入方式公开单 Job 工厂、批次控制器、原子状态存储和 Agent 工作区，避免运行时与
工作流包初始化互相加载；不实现模型协议或 hidden verifier。
主要接口：``ScienceAgentFactory``、``BatchController``、``JobStateStore``、``BatchStateStore``、
``AgentWorkspace`` 和 ``WorkspaceManager``。
输入：``FactorySettings``、SeedBundle 路径、Job/Batch ID 和 Agent 结构化交付。
输出：Job/Batch 状态摘要、归档制品和可恢复的原子 JSON 状态。
副作用与边界：读取具体导出属性时才加载对应模块；实际对象可创建工作目录、运行 Agent 并更新状态，
模型调用和验证分别委托给 agents 与 verification。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AgentWorkspace",
    "BatchController",
    "BatchStateStore",
    "JobStateStore",
    "ScienceAgentFactory",
    "WorkspaceManager",
]

_EXPORTS = {
    "AgentWorkspace": (".workspace", "AgentWorkspace"),
    "BatchController": (".batch", "BatchController"),
    "BatchStateStore": (".batch", "BatchStateStore"),
    "JobStateStore": (".state", "JobStateStore"),
    "ScienceAgentFactory": (".factory", "ScienceAgentFactory"),
    "WorkspaceManager": (".workspace", "WorkspaceManager"),
}


def __getattr__(name: str) -> Any:
    """按需加载公共对象，同时保持原有 ``workflow`` 导入接口。"""

    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
