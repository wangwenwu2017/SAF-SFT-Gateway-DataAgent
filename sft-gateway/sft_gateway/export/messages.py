"""导出重建后的消息快照；导出器不自行重建历史。

输入是 trajectory builder 已经按前缀链接好的 ``Trajectory`` 对象，输出是
可 JSON 序列化的普通字典。``export_samples`` 按请求去重，保留 context/target
原始 item；``export_trajectories`` 每条根到叶路径输出一行，并标记是否完整。
这里的 ``messages`` 是通用内部格式，Codex ``sample.json`` 兼容格式由
``export.codex`` 负责。
"""

from copy import deepcopy
from typing import Any, Iterable

from ..gateway.detection import Protocol
from ..protocols.responses import ResponsesAdapter
from ..trajectory.models import CompletedCompletion, Trajectory


def _messages(turn: CompletedCompletion) -> list[dict[str, Any]]:
    """把一个完成请求的 context+target 映射成消息列表，不修改原对象。"""
    items = turn.context + turn.target
    if turn.protocol == Protocol.RESPONSES:
        return ResponsesAdapter().items_to_messages(items)
    return deepcopy(items)


def _turn_metadata(turn: CompletedCompletion) -> dict[str, Any]:
    """只读输入完成记录，输出解释匹配、父节点及捕获状态的元数据字典。

    不含完整请求内容，不查询存储，也不重新推断链接关系。
    """
    return {
        "source_request_id": turn.request_id,
        "response_id": turn.response_id,
        "previous_response_id": turn.previous_response_id,
        "session_key": turn.session_key,
        "parent_request_id": turn.parent_request_id,
        "link_method": turn.link_method,
        "matched_prompt_items": turn.prefix_length,
        "context_item_count": len(turn.context),
        "target_item_count": len(turn.target),
        "capture_status": turn.capture_status,
        "recovered_from_events": turn.recovered_from_events,
    }


def export_samples(trajectories: Iterable[Trajectory]) -> Iterable[dict[str, Any]]:
    """按请求生成唯一样本，即使多个叶路径共享同一祖先。

    输出包含 ``sample_id``、轨迹/请求元数据、``messages``、``context_items``、
    ``target_items`` 和请求工具声明；messages 的末尾包含可投影的监督目标。
    未知/自定义 Responses item 可能不出现在 messages 中，但仍保存在原始 item
    字段；本函数不额外按 pending 工具调用或 break_reason 过滤轨迹。
    输入轨迹迭代器，输出按首次出现顺序去重的字典迭代器；在内存中建立路径
    归属表，不修改轨迹或写文件。此接口用于审计，不是默认训练集资格过滤器。
    """
    paths = list(trajectories)
    memberships: dict[tuple[str, str], list[str]] = {}
    for trajectory in paths:
        for turn in trajectory.turns:
            memberships.setdefault((turn.run_id, turn.request_id), []).append(trajectory.trajectory_id)
    seen: set[tuple[str, str]] = set()
    for trajectory in paths:
        for index, turn in enumerate(trajectory.turns):
            key = (turn.run_id, turn.request_id)
            if key in seen:
                continue
            seen.add(key)
            yield {
                "sample_id": f"sample_{turn.request_id}",
                "trajectory_id": trajectory.trajectory_id,
                "trajectory_ids": memberships[key],
                "turn_index": index,
                "run_id": turn.run_id,
                "protocol": turn.protocol.value,
                "model": turn.model,
                "messages": _messages(turn),
                "context_items": deepcopy(turn.context),
                "target_items": deepcopy(turn.target),
                "tools": deepcopy(turn.request.get("tools", [])),
                **_turn_metadata(turn),
                "break_reason": trajectory.break_reason,
            }


def export_trajectories(trajectories: Iterable[Trajectory]) -> Iterable[dict[str, Any]]:
    """按叶节点的完整 context 输出一条根到叶轨迹快照。

    ``complete`` 仅表示 trajectory builder 没有设置 break_reason，并不额外保证
    所有工具调用都已返回；Codex SFT 导出会再做 pending tool call 检查。
    输入轨迹迭代器，输出包含原生 items 和投影 messages 的字典迭代器；
    跳过空轨迹，不修改输入或执行 I/O。complete 不检查不支持的上下文类型。
    """
    for trajectory in trajectories:
        if not trajectory.turns:
            continue
        leaf = trajectory.turns[-1]
        yield {
            "trajectory_id": trajectory.trajectory_id,
            "run_id": trajectory.run_id,
            "protocol": trajectory.protocol.value,
            "model": trajectory.model,
            "messages": _messages(leaf),
            "items": deepcopy(leaf.context + leaf.target),
            "tools": deepcopy(leaf.request.get("tools", [])),
            "turns": [_turn_metadata(turn) for turn in trajectory.turns],
            "source_request_ids": [turn.request_id for turn in trajectory.turns],
            "response_ids": [turn.response_id for turn in trajectory.turns],
            "complete": trajectory.break_reason is None,
            "break_reason": trajectory.break_reason,
        }
