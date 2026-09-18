"""校验工具调用因果关系，保持请求中出现的顺序而不重排工具结果。

输入是叶轨迹的原生 context+target 和协议；输出包含未返回的 call_id、
缺失/重复/孤立/类型不匹配问题，以及是否曾有多个调用同时等待结果。
这只验证工具 API 的调用闭合，不推断 shell 后台进程或整个任务是否完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..gateway.detection import Protocol
from ..trajectory.models import Trajectory


@dataclass
class ToolValidation:
    """一次只读工具审计结果；``complete`` 要求无未决调用且无结构错误。"""

    pending_call_ids: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    has_parallel_calls: bool = False

    @property
    def complete(self) -> bool:
        """只读返回是否无 pending ID 和结构问题；不代表任务执行成功。"""
        return not self.pending_call_ids and not self.issues


def validate_tool_calls(
    items: Iterable[Mapping[str, Any]], *, protocol: Protocol,
) -> ToolValidation:
    """按 call_id 校验 function/custom 或 Chat 工具；允许已发调用乱序返回。

    Responses 的 item.id 不是 call_id，不用它补造关联。任何结构错误都会
    使结果不完整；未知 provider 内置工具不在这项 function/custom 校验内。
    输入有序 item 迭代器和协议，输出 ToolValidation；只遍历一次且不修改
    item。多个工具尚未返回时允许出现 assistant、新调用以及乱序结果，不重排。
    """
    result = ToolValidation()
    pending: dict[str, str] = {}
    seen_calls: set[str] = set()
    seen_outputs: set[str] = set()

    def problem(kind: str, index: int, call_id: Any) -> None:
        """输入错误类别、item 位置和 ID，追加内部问题列表；返回 None。"""
        result.issues.append({"kind": kind, "item_index": index, "call_id": call_id})

    def event(kind: str, call_id: Any, family: str, index: int) -> None:
        """输入调用/结果事件及工具类型，更新 pending/去重状态；返回 None。

        只维护本次校验的局部集合与问题列表，不执行工具或修改原始 items。
        """
        if not isinstance(call_id, str) or not call_id:
            problem("missing_call_id" if kind == "call" else "missing_output_call_id", index, call_id)
            return
        if kind == "call":
            if call_id in seen_calls:
                problem("duplicate_call_id", index, call_id)
                return
            if pending:
                result.has_parallel_calls = True
            seen_calls.add(call_id)
            pending[call_id] = family
            return
        if call_id in seen_outputs:
            problem("duplicate_tool_output", index, call_id)
            return
        seen_outputs.add(call_id)
        if call_id not in pending:
            problem("orphan_tool_output", index, call_id)
        elif pending[call_id] != family:
            problem("tool_type_mismatch", index, call_id)
        else:
            del pending[call_id]

    for index, item in enumerate(items):
        if protocol == Protocol.RESPONSES:
            kind = item.get("type")
            if kind in {"function_call", "custom_tool_call"}:
                event("call", item.get("call_id"), str(kind), index)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                event("output", item.get("call_id"), str(kind).removesuffix("_output"), index)
        else:
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    event("call", call.get("id"), "chat", index)
                else:
                    problem("invalid_tool_call", index, None)
            if item.get("role") == "tool":
                event("output", item.get("tool_call_id"), "chat", index)
    result.pending_call_ids = list(pending)
    return result


def validate_trajectory_tools(trajectory: Trajectory) -> ToolValidation:
    """校验非空叶快照；空轨迹返回结构错误，不能作为完整 SFT 数据。"""
    if not trajectory.turns:
        return ToolValidation(issues=[{"kind": "empty_trajectory", "item_index": None, "call_id": None}])
    leaf = trajectory.turns[-1]
    return validate_tool_calls(leaf.context + leaf.target, protocol=leaf.protocol)
