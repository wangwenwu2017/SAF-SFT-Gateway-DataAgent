"""Responses SSE 流的有序重建状态机。

上游可能以 ``response.*``、output item、content part、文本 delta 和工具
参数 delta 的多个 SSE 事件返回同一响应。本模块按 ``output_index`` 或
``item_id`` 聚合这些事件，在 ``finalize`` 时生成一个可持久化的快照。
它只覆盖当前代码认识的事件类型；未知事件会被忽略但不会阻断流，因而
原始 SSE 日志仍应作为审计依据。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResponsesSnapshot:
    """一次流重建的语义结果容器（字段本身为普通可序列化对象）。

    ``response`` 是尽量完整的 Responses 响应，``output_items`` 是其输出
    列表副本；``errors`` 保存失败/error 事件。快照不携带原始 SSE 行，也
    不承诺恢复服务端未发送的字段。返回对象不是 frozen dataclass；output
    已复制，但 usage/errors 及 response 的部分元数据可能共享状态机引用，
    调用者若要长期隔离保存应 deepcopy 整个结果。
    """
    response_id: str | None
    status: str
    response: dict[str, Any] | None
    output_items: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


class ResponsesStreamState:
    """从已解码的 SSE event 字典重建一个 Responses 响应。

    输入通过 ``consume`` 逐事件提供，调用者负责解析 SSE 和决定何时结束；
    输出由 ``finalize`` 产生。状态机按 item ID/index 保持输出顺序，并把
    文本、reasoning summary、function-call arguments 的 delta 做字符串
    拼接。它不能推断缺失事件、修复乱序且无法解密 opaque 内容。
    """

    def __init__(self) -> None:
        """无需输入，分配一个响应的内存累积状态并返回 None；没有外部 I/O。

        每个上游响应应使用独立实例，不能混入其他响应事件或并发调用 consume。
        """
        self.response: dict[str, Any] = {}
        self.items: dict[int, dict[str, Any]] = {}
        self.order: list[int] = []
        self.item_ids: dict[str, int] = {}
        self.response_id: str | None = None
        self.status = "unknown"
        self.usage: dict[str, Any] | None = None
        self.errors: list[dict[str, Any]] = []
        self.done_items: dict[int, dict[str, Any]] = {}
        self.invalid_done_sequence = False

    def _index(self, event: dict[str, Any], item: dict[str, Any] | None = None) -> int:
        """输入事件及可选 item，返回 output 索引并登记 ID、占位 item。

        优先显式 output_index，再查 item ID；都没有时分配新索引。会修改
        内部索引表，不推断缺少标识的两个事件是否属于同一项。
        """
        value = event.get("output_index")
        if not isinstance(value, int):
            value = None
        item_id = event.get("item_id")
        if not isinstance(item_id, str) and isinstance(item, dict):
            item_id = item.get("id") if isinstance(item.get("id"), str) else None
        if value is None and item_id is not None:
            value = self.item_ids.get(item_id)
        if value is None:
            value = max(self.items, default=-1) + 1
        if item_id is not None:
            self.item_ids[item_id] = value
        if value not in self.items:
            self.items[value] = {}
            self.order.append(value)
        return value

    def _item(self, event: dict[str, Any], default_type: str = "message") -> dict[str, Any]:
        """输入事件及默认类型，返回内部可变 item 引用并合并已提供字段。

        会修改本状态机，服务端 item 字段在写入时复制；不修改事件对象。
        """
        supplied = event.get("item") if isinstance(event.get("item"), dict) else None
        index = self._index(event, supplied)
        current = self.items[index]
        if supplied:
            for key, value in supplied.items():
                current[key] = deepcopy(value)
            if isinstance(supplied.get("id"), str):
                self.item_ids[supplied["id"]] = index
        current.setdefault("type", default_type)
        if current.get("type") == "message":
            current.setdefault("role", "assistant")
            current.setdefault("content", [])
        return current

    @staticmethod
    def _part(parts: list[dict[str, Any]], index: int, default: dict[str, Any]) -> dict[str, Any]:
        """输入 parts、索引、字段映射，原地扩展列表并返回该位置的字典。

        default 会覆盖同名字段；仅接受已有位置或紧接列表末尾的位置。
        非法容器或稀疏/负数索引抛 ValueError，避免按不可信索引分配巨量占位。
        """
        if not isinstance(parts, list) or type(index) is not int or not 0 <= index <= len(parts):
            raise ValueError("invalid content part index or container")
        if len(parts) == index:
            parts.append({})
        if not isinstance(parts[index], dict):
            parts[index] = {}
        parts[index].update(default)
        return parts[index]

    def consume(self, event: dict[str, Any] | None) -> None:
        """输入 JSON 事件或 None，累积响应/item/delta 状态并返回 None。

        不修改输入、不执行 I/O；非字典被忽略，未知事件仍可更新公共响应
        元数据。仅支持下列分支中的事件形状，不校验所有字段及索引合法性。
        """
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        response = event.get("response")
        if isinstance(response, dict):
            self.response.update(deepcopy(response))
            self.response_id = response.get("id") or self.response_id
            self.status = response.get("status") or self.status
            if isinstance(response.get("usage"), dict):
                self.usage = deepcopy(response["usage"])
        self.response_id = event.get("response_id") or self.response_id
        if kind == "response.completed":
            self.status = "completed"
        elif kind == "response.incomplete":
            self.status = "incomplete"
        elif kind in {"response.failed", "error"}:
            self.status = "failed"
            self.errors.append(deepcopy(event))
        if isinstance(event.get("usage"), dict):
            self.usage = deepcopy(event["usage"])

        item = event.get("item")
        if kind in {"response.output_item.added", "response.output_item.done"} and isinstance(item, dict):
            current = self._item(event)
            if kind == "response.output_item.done":
                # Reuse the index assigned above, including anonymous items;
                # allocating twice would change the original capture snapshot.
                index = next(index for index, value in self.items.items() if value is current)
                self.invalid_done_sequence |= index in self.done_items or self.status in {"completed", "failed", "incomplete"}
                self.done_items[index] = deepcopy(item)
            return
        if kind in {"response.content_part.added", "response.content_part.done"}:
            current = self._item(event)
            content_index = event.get("content_index", 0)
            content_index = content_index if isinstance(content_index, int) else 0
            part = event.get("part")
            if isinstance(part, dict):
                self._part(current.setdefault("content", []), content_index, deepcopy(part))
            return
        if kind in {"response.output_text.delta", "response.output_text.done", "response.reasoning_text.delta", "response.reasoning_text.done"}:
            is_reasoning = kind.startswith("response.reasoning_text.")
            current = self._item(event, "reasoning" if is_reasoning else "message")
            content_index = event.get("content_index", 0)
            content_index = content_index if isinstance(content_index, int) else 0
            # Defaults must not erase earlier deltas in the same content part.
            part = self._part(current.setdefault("content", []), content_index, {"type": "reasoning_text" if is_reasoning else "output_text"})
            if kind.endswith(".delta") and isinstance(event.get("delta"), str):
                part["text"] = part.get("text", "") + event["delta"]
            elif isinstance(event.get("text"), str):
                part["text"] = event["text"]
            return
        if kind in {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done", "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}:
            current = self._item(event, "reasoning")
            summary_index = event.get("summary_index", 0)
            summary_index = summary_index if isinstance(summary_index, int) else 0
            summary = current.setdefault("summary", [])
            part = event.get("part")
            if isinstance(part, dict):
                self._part(summary, summary_index, deepcopy(part))
            else:
                text = event.get("delta") if kind.endswith(".delta") else event.get("text")
                target = self._part(summary, summary_index, {"type": "summary_text"})
                if isinstance(text, str):
                    target["text"] = target.get("text", "") + text if kind.endswith(".delta") else text
            return
        if kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            current = self._item(event, "function_call")
            if kind.endswith(".delta") and isinstance(event.get("delta"), str):
                current["arguments"] = current.get("arguments", "") + event["delta"]
            elif isinstance(event.get("arguments"), str):
                current["arguments"] = event["arguments"]
        if kind in {"response.custom_tool_call_input.delta", "response.custom_tool_call_input.done"}:
            current = self._item(event, "custom_tool_call")
            if kind.endswith(".delta") and isinstance(event.get("delta"), str):
                current["input"] = current.get("input", "") + event["delta"]
            elif isinstance(event.get("input"), str):
                current["input"] = event["input"]

    def finalize(self) -> ResponsesSnapshot:
        """无需输入，返回 ResponsesSnapshot；不清空状态或标记网络流完成。

        优先使用响应携带的非空 output，否则按 output_index 重建。输出 items
        深拷贝；usage/errors 及部分响应元数据仍可能共享引用，见结果类型说明。
        """
        reconstructed = [self.items[index] for index in sorted(self.items)]
        advertised = self.response.get("output")
        output = [item for item in advertised if isinstance(item, dict)] if isinstance(advertised, list) and advertised else reconstructed
        response = dict(self.response) if self.response else None
        if response is not None or output:
            response = response or {}
            response["output"] = deepcopy(output)
            response.setdefault("id", self.response_id)
            response.setdefault("status", self.status)
        return ResponsesSnapshot(self.response_id, self.status, response, deepcopy(output), self.usage, self.errors)

    def replay_response(self) -> dict[str, Any] | None:
        """从已记录 SSE 构造客户端回放视图，原始终态快照保持不变。

        使用完整、顺序明确的 output_item.done；只有 reasoning 的两份非空
        密文可不同，仍要求 item ID、正文和工具参数一致。缺项、重复、
        重排或可见内容冲突抛 ValueError，供导出器隔离该记录。
        """
        from ..trajectory.conversation_index import _canonical_item

        response = deepcopy(self.finalize().response)
        if not self.done_items or response is None:
            return response
        indices = sorted(self.done_items)
        if self.invalid_done_sequence or indices != list(range(len(indices))) or set(self.items) != set(indices):
            raise ValueError("incomplete or duplicate output_item.done sequence")
        done = [self.done_items[index] for index in indices]
        advertised = self.response.get("output")
        if isinstance(advertised, list) and advertised:
            if len(advertised) != len(done):
                raise ValueError("output_item.done count differs from terminal output")
            for item, final in zip(done, advertised, strict=True):
                if not isinstance(final, dict) or item.get("id") != final.get("id"):
                    raise ValueError("output_item.done identity differs from terminal output")
                left, right = _canonical_item(item), _canonical_item(final)
                if (item.get("type") == "reasoning" and final.get("type") == "reasoning"
                        and isinstance(item.get("id"), str) and item["id"]
                        and all(isinstance(x.get("encrypted_content"), str) and x["encrypted_content"] for x in (left, right))):
                    # Select the observed client-consumed ciphertext; never
                    # normalize ciphertext during cross-request matching.
                    right["encrypted_content"] = left["encrypted_content"]
                if left != right:
                    raise ValueError("output_item.done content conflicts with terminal output")
        response["output"] = deepcopy(done)
        return response
