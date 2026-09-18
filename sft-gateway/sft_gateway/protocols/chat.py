"""从 Chat 请求/响应提取有序消息，不请求上游、不保存状态或执行工具。

只消费首个 choice；返回的新列表可能引用输入中的字典，调用者若需修改
嵌套值必须自行复制。有效完成状态由捕获层判断，本适配器不校验 finish_reason。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..gateway.detection import Protocol


class ChatAdapter:
    """OpenAI Chat Completions 风格协议的只读适配器。

    请求输入来自 ``messages``，响应目标取第一个 choice 的 ``message``。
    ``to_messages`` 只做列表拼接，不改变输入对象，也不推断缺失的工具
    调用；因此它适合保留黑盒 harness 实际发送的消息顺序。
    """

    protocol = Protocol.CHAT

    def response_id(self, value: Mapping[str, Any] | None) -> str | None:
        """从响应 JSON 读取字符串 ``id``；格式不符时返回 ``None``。"""
        rid = (value or {}).get("id")
        return rid if isinstance(rid, str) else None

    def previous_response_id(self, request: Mapping[str, Any]) -> str | None:
        """只读输入请求 JSON，输出字符串父响应 ID 或 None。

        此字段是兼容扩展；不表示 Chat Completions 标准会保存响应历史。
        """
        value = request.get("previous_response_id")
        return value if isinstance(value, str) else None

    def extract_context(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        """返回请求 ``messages`` 中的字典项，保持原有顺序。"""
        value = request.get("messages", [])
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def extract_target(self, response: Mapping[str, Any]) -> list[dict[str, Any]]:
        """返回第一个 choice 的 message；没有合法 choice 时返回空列表。"""
        choices = response.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        return [message] if isinstance(message, dict) else []

    def to_messages(self, request: Mapping[str, Any], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把请求上下文和本次目标拼成新的列表，不修改两侧输入。"""
        return self.extract_context(request) + target
