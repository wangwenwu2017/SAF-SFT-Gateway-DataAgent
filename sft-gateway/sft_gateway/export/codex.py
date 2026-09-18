"""把完整轨迹导出为 Codex harness 的 SFT schema。

The target schema is the one used by ``sample.json``::

    {"task": str, "tools": str, "messages": [{"role": str, "content": str}]}

本格式每条叶轨迹输出一行，工具调用和工具结果会分别映射成 harness
message。当前转换会把 ``developer`` 角色改为
示例格式使用的 ``system``；未知或 opaque Responses item 不会伪造为 assistant
文本。可读 reasoning 按示例包裹为 assistant 的 ``<think>`` 内容，优先正文，
正文为空时使用 provider summary；纯密文不导出为文本。该格式是一种有损
文本投影：并行工具在 content 中携带 call_id，phase 和图像等非文本块会省略；原始 item 应通过
内部/native 归档审计。工具消息角色还包括 ``tool_call`` 和 ``tool_response``。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable, Mapping

from ..gateway.detection import Protocol
from ..trajectory.models import Trajectory
from .validation import validate_tool_calls, validate_trajectory_tools


def validate_tool_calls_for_items(items: Iterable[Mapping[str, Any]], protocol: Protocol):
    """只读遍历有序 items，按协议返回 ToolValidation；不缓存或修改输入。

    让投影中的并行 ID 保留策略与工具完整性过滤使用同一个校验器。
    """
    return validate_tool_calls(items, protocol=protocol)


def _unresolved_tool_calls(items: Iterable[Mapping[str, Any]]) -> set[str]:
    """返回叶快照中没有匹配结果的工具调用 ID。

    Responses 可以一次包含多个 function/custom tool call，结果也可能乱序返回；
    按 call_id 匹配可识别调用后乱序返回的结果。它检查 function/custom tool，
    不验证未知 provider 工具类型，也不推断工具进程是否还在后台运行。
    输入 item 迭代器，输出 ID 集合；只读。此旧 helper 会回退 item.id，且不
    检测重复/孤立结果；当前训练过滤使用严格的 validate_trajectory_tools。
    """
    pending: set[str] = set()
    for item in items:
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id") or item.get("id")
            if isinstance(call_id, str) and call_id:
                pending.add(call_id)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                pending.discard(call_id)
    return pending


def has_unresolved_tool_calls(trajectory: Trajectory) -> bool:
    """输入轨迹，返回其叶快照是否存在未决调用或工具结构错误；只读。

    名称中的 unresolved 也包含重复 ID、孤立结果等错误，不只表示缺少返回。
    """
    return not validate_trajectory_tools(trajectory).complete


def _text(value: Any) -> str:
    """只读输入 JSON 值，输出文本；列表拼接 text/content/refusal，跳过非文本块。

    不插入块分隔符；映射会 JSON 序列化，None 为空。该投影不保留图像、音频
    及未知块的上下文语义，调用方不能据此判断样本是否完整。
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                elif isinstance(item.get("refusal"), str):
                    parts.append(item["refusal"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _tool_call(item: Mapping[str, Any], *, preserve_call_id: bool = False, call_id: str | None = None) -> dict[str, str]:
    """只读输入工具 item 及 ID 保留选项，输出 role/content 消息字典。

    arguments/input 若是合法 JSON 文本则先解析；自定义工具的自由文本保持字符串。
    content 包含 name 和 arguments；preserve_call_id=True 时也嵌入 call_id。
    不执行工具或校验工具 schema；其余原始字段需查归档。
    """
    arguments = item.get("arguments", item.get("input", ""))
    if isinstance(arguments, str) and item.get("type") != "custom_tool_call":
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    payload = {
        "name": item.get("name", ""),
        "arguments": arguments,
    }
    if preserve_call_id and isinstance(call_id or item.get("call_id"), str):
        payload["call_id"] = call_id or item["call_id"]
    return {
        "role": "tool_call",
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _reasoning_text(item: Mapping[str, Any]) -> str:
    """读取一个 reasoning item 的可读文本，正文优先、摘要兜底。

    输入为原始 Responses reasoning item；输出只包含 provider 提供的文本。
    多个已完成 content/summary block 用换行分隔，不把正文和摘要重复拼接，
    也不尝试解密 encrypted_content。事件流的 delta 应在协议层先重建成 block。
    """
    for field in ("content", "summary"):
        value = item.get(field)
        if isinstance(value, str):
            text = value
        elif isinstance(value, list):
            text = "\n".join(
                part["text"]
                for part in value
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
            )
        else:
            text = ""
        if text.strip():
            return text
    return ""


def _codex_tools(value: Any) -> Any:
    """规范化工具声明并保留非 function 工具语义。

    sample schema 把 tools 编码成 JSON 文本，因此 namespace/provider 声明保持原样；
    namespace 不是可调用函数，强行转换会丢失嵌套工具和 schema。
    """
    if not isinstance(value, list):
        return value
    normalized: list[Any] = []
    for tool in value:
        if not isinstance(tool, dict):
            normalized.append(deepcopy(tool))
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            normalized.append(deepcopy(tool))
            continue
        if tool.get("type") == "function" and isinstance(tool.get("name"), str):
            function = {key: deepcopy(item) for key, item in tool.items() if key != "type"}
            normalized.append({"type": "function", "function": function})
            continue
        normalized.append(deepcopy(tool))
    return normalized


def _item_messages(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """把 Responses item 投影为扁平 Codex harness messages。

    当同一快照中有多个同时未返回的调用时，sample schema 没有独立
    ``call_id`` 字段，因此把它嵌入 tool_call/tool_response 的 JSON content；
    串行调用继续保持 sample.json 的原有简洁格式。工具结果严格保留 wire 顺序。
    输入有序 items，输出新消息列表，不修改 items。未知类型、opaque 内容和
    非文本块可能被省略；本函数不将这种投影损失上报为不合格样本。
    """
    messages: list[dict[str, str]] = []
    audit_items = list(items)
    preserve_call_ids = validate_tool_calls_for_items(audit_items, Protocol.RESPONSES).has_parallel_calls
    for item in audit_items:
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            messages.append(_tool_call(item, preserve_call_id=preserve_call_ids))
            continue
        if kind in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output", "")
            if preserve_call_ids and isinstance(item.get("call_id"), str):
                output = json.dumps({"call_id": item["call_id"], "output": output}, ensure_ascii=False)
            messages.append({"role": "tool_response", "content": _text(output)})
            continue
        if kind == "reasoning":
            # sample.json distinguishes reasoning from the visible answer by
            # wrapping it in a separate assistant <think> message.
            text = _reasoning_text(item)
            if text:
                messages.append({"role": "assistant", "content": f"<think>\n{text}\n</think>"})
            continue
        if kind == "message" or isinstance(item.get("role"), str):
            role = item.get("role") if isinstance(item.get("role"), str) else "assistant"
            if role == "developer":
                role = "system"
            content = _text(item.get("content", ""))
            if not content:
                # Responses refusal parts carry their text in ``refusal``
                # rather than ``text``. Keep the refusal visible instead of
                # exporting an empty assistant target.
                content = _text(item.get("refusal", ""))
            messages.append({"role": role, "content": content})
            continue
        # Unknown provider items (including compaction/opaque records) have no
        # established SFT role. Serializing them as assistant text invents a
        # model target and may train on encrypted metadata, so retain them in
        # native archives only and omit them from this text projection.
    return messages


def _chat_messages(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """把 Chat messages/tool_calls 投影为 sample.json 的消息格式。

    并行 Chat tool_calls 同样只在需要时在 JSON content 中保留 ID，并保持
    服务端返回顺序；不会按调用声明顺序重排结果。
    输入 Chat 历史，输出新列表，不修改输入；仅处理 content 和 tool_calls，
    reasoning_content、refusal 等独立扩展字段不在当前投影范围内。
    """
    messages: list[dict[str, str]] = []
    audit_items = list(items)
    preserve_call_ids = validate_tool_calls_for_items(audit_items, Protocol.CHAT).has_parallel_calls
    for item in audit_items:
        role = item.get("role") if isinstance(item.get("role"), str) else "assistant"
        if role == "developer":
            role = "system"
        if role == "tool":
            content: Any = item.get("content", "")
            if preserve_call_ids and isinstance(item.get("tool_call_id"), str):
                content = json.dumps({"call_id": item["tool_call_id"], "output": content}, ensure_ascii=False)
            messages.append({"role": "tool_response", "content": _text(content)})
            continue
        calls = item.get("tool_calls")
        content = _text(item.get("content", ""))
        if content:
            messages.append({"role": role, "content": content})
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else call
                messages.append(_tool_call(function, preserve_call_id=preserve_call_ids, call_id=call.get("id")))
        elif not content:
            messages.append({"role": role, "content": content})
    return messages


def trajectory_to_codex_sample(
    trajectory: Trajectory,
    *,
    task: str | None = None,
    tools: Any = None,
) -> dict[str, Any]:
    """把一条轨迹转换为 ``sample.json`` 的单个对象。

    输入为非空 trajectory，可选 task/tools 覆盖自动取值；空轨迹抛 ValueError。
    task 默认取叶请求的 task 或 metadata.task，否则回溯祖先的 task 字段，最后
    使用 run ID。tools 默认仅取叶请求声明的 tools，序列化成 JSON 字符串；
    请求的 additional_tools 不会自动合并。输出只含 task/tools/messages 三个字段。
    本函数不校验轨迹完整性；需要 pending 检查时调用 export_codex_samples。
    只读取叶 context+target，不逐轮重新拼接或验证 tools 历史；工具 schema
    若中途变化，单一 tools 字段可能无法覆盖此前调用。无外部 I/O。
    """
    if not trajectory.turns:
        raise ValueError("cannot export an empty trajectory")
    leaf = trajectory.turns[-1]
    request = leaf.request
    if task is None:
        task_value = request.get("task")
        metadata = request.get("metadata")
        if not isinstance(task_value, str) and isinstance(metadata, dict):
            task_value = metadata.get("task")
        if not isinstance(task_value, str) or not task_value:
            for turn in reversed(trajectory.turns[:-1]):
                candidate = turn.request.get("task")
                if isinstance(candidate, str) and candidate:
                    task_value = candidate
                    break
        task = task_value if isinstance(task_value, str) and task_value else trajectory.run_id
    if tools is None:
        tools = request.get("tools", [])
    if isinstance(tools, str):
        tools_text = tools
    else:
        tools_text = json.dumps(_codex_tools(tools or []), ensure_ascii=False)
    items = leaf.context + leaf.target
    if leaf.protocol == Protocol.RESPONSES:
        messages = _item_messages(items)
    else:
        messages = _chat_messages(items)
    return {"task": task, "tools": tools_text, "messages": messages}


def export_codex_samples(
    trajectories: Iterable[Trajectory],
    *,
    task: str | None = None,
    include_incomplete: bool = False,
    include_broken: bool = False,
) -> Iterable[dict[str, Any]]:
    """输入轨迹，惰性输出 task/tools/messages 样本，不写文件。
    默认通过统一 rejection_reasons 检查；仅同时设置两个调试开关才允许输出被拒绝样本。
    资格检查包含工具闭合、上下文稳定、可投影类型、观测监督来源和非空目标；不证明任务成功。"""
    from .eligibility import rejection_reasons
    for trajectory in trajectories:
        reasons = rejection_reasons(trajectory)
        if reasons and not (include_incomplete and include_broken):
            continue
        yield trajectory_to_codex_sample(trajectory, task=task)
