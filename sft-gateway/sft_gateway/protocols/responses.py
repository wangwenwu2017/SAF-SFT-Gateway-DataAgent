"""OpenAI Responses 协议的规范化适配器。

本模块把 ``instructions``、``input`` 和响应 ``output`` 转成轨迹索引需要
的有序字典列表，并提供一个有限的 Chat-style 投影供 SFT 导出使用。投影
不会声称能表达所有 Responses item：reasoning、加密内容和未知 item 会在
原始 ``context_items``/``target_items`` 中保留，调用方不应只依赖投影消息。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping

from ..gateway.detection import Protocol


def _items(value: Any) -> list[dict[str, Any]]:
    """把 Responses 的字符串、对象或对象列表规范化为对象列表。

    输入中的非字典、非字符串项被忽略；字符串被视为用户 message。该
    处理只建立新列表，不修改原始请求。
    """
    if isinstance(value, str):
        return [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": value}]}]
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(item)
        elif isinstance(item, str):
            items.extend(_items(item))
    return items


class ResponsesAdapter:
    """Responses API 的只读字段提取和消息投影实现。

    ``extract_context`` 保留请求级 ``instructions`` 作为一个或多个 system 项，
    再按 wire 顺序追加 ``input``。``extract_target`` 保留 response output
    item 的原始字典，工具调用的并行关系只在 ``items_to_messages`` 中做
    连续 function_call 的 Chat 投影。
    """

    protocol = Protocol.RESPONSES

    def response_id(self, value: Mapping[str, Any] | None) -> str | None:
        """只读输入响应映射或 None，返回字符串 id；不存在或类型不符时返回 None。"""
        item = value or {}
        rid = item.get("id")
        return rid if isinstance(rid, str) else None

    def previous_response_id(self, request: Mapping[str, Any]) -> str | None:
        """只读输入请求映射，返回字符串 previous_response_id 或 None；不查找父节点。"""
        value = request.get("previous_response_id")
        return value if isinstance(value, str) else None

    def extract_context(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        """输入请求 JSON，输出本次 instructions 和 input 的有序 item 列表。

        instructions 支持字符串及兼容扩展数组；数组按顺序映射为 system 项。
        本函数不修改请求，不读取 previous_response_id 的历史；instruction
        字典复制，而 input 字典沿用引用，因此返回结果不是完整的深拷贝。
        """
        context: list[dict[str, Any]] = []
        instructions = request.get("instructions")
        if isinstance(instructions, str) and instructions:
            context.append({"role": "system", "content": instructions})
        elif isinstance(instructions, list):
            for item in instructions:
                if isinstance(item, dict):
                    instruction = deepcopy(item)
                    # Some compatible providers encode an instruction array
                    # as content parts (``{"type":"input_text",...}``)
                    # rather than message objects. Preserve the part under a
                    # system message so it remains usable in SFT messages.
                    if "role" not in instruction and "content" not in instruction and "text" in instruction:
                        instruction = {"role": "system", "content": [instruction]}
                    else:
                        instruction.setdefault("role", "system")
                    context.append(instruction)
                elif isinstance(item, str):
                    context.append({"role": "system", "content": item})
                else:
                    context.append({"role": "system", "content": deepcopy(item)})
        return context + _items(request.get("input", []))

    def extract_target(self, response: Mapping[str, Any]) -> list[dict[str, Any]]:
        """输入响应 JSON，返回 output 中的字典引用列表，保持顺序和字段。

        不修改响应，也不判断 status、未知 item 是否可训练；要求 output 可迭代。
        """
        return [item for item in response.get("output", []) if isinstance(item, dict)]

    def to_messages(self, request: Mapping[str, Any], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把请求上下文及本次输出投影为 Chat-style 消息列表。"""
        return self.items_to_messages(self.extract_context(request) + target)

    def items_to_messages(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 Responses item 投影成 Chat-style SFT 消息。

        连续的 ``function_call``/``custom_tool_call`` item 会合并到同一个
        assistant ``tool_calls`` 数组，以表达并行调用；两类 output 都保留
        ``tool_call_id``。可读 reasoning 文本映射为 assistant 内容，加密
        reasoning 和未知 item 仍只在原始 item 快照中保留。
        输入为有序原生 items，输出新消息列表；不修改输入或执行 I/O。此处
        reasoning 先选摘要再选正文，与 Codex 文本投影的正文优先策略不同。
        """
        messages: list[dict[str, Any]] = []
        previous_kind = None
        for item in items:
            kind = item.get("type")
            if kind == "message" or (kind is None and isinstance(item.get("role"), str)):
                role = item.get("role", "assistant")
                message = {"role": role, "content": _content(item.get("content", []))}
                if item.get("phase") is not None:
                    message["phase"] = item["phase"]
                messages.append(message)
            elif kind in {"function_call", "custom_tool_call"}:
                call = {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", item.get("input", "")),
                    },
                }
                if previous_kind in {"function_call", "custom_tool_call"}:
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                output = item.get("output", "")
                messages.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)})
            elif kind == "reasoning":
                summary = item.get("summary")
                content = item.get("content")
                text = _reasoning_text(summary) or _reasoning_text(content)
                if text:
                    messages.append({"role": "assistant", "content": text})
            previous_kind = kind
        return messages


def _content(value: Any) -> Any:
    """把 Responses content parts 转成文本或 Chat 多模态块。

    纯文本 parts 合并为字符串；图片 ``input_image`` 映射为
    ``image_url``。未知 part 深拷贝后原样保留，避免为了投影丢失数据。
    """
    if not isinstance(value, list):
        return deepcopy(value)
    if all(isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"} and set(part) <= {"type", "text"} for part in value):
        return "".join(part.get("text", "") for part in value)
    blocks = []
    for part in value:
        part = deepcopy(part)
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}:
            part["type"] = "text"
        elif isinstance(part, dict) and part.get("type") == "input_image" and "image_url" in part:
            image_url = {"url": part["image_url"]}
            if "detail" in part:
                image_url["detail"] = part["detail"]
            part = {"type": "image_url", "image_url": image_url}
        blocks.append(part)
    return blocks


def _reasoning_text(value: Any) -> str:
    """只读输入字符串或 reasoning parts，返回连接的 text；无法投影则为空。

    不加入块分隔符，也不解密 encrypted_content；摘要/正文取舍由调用方决定。
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in value
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )
