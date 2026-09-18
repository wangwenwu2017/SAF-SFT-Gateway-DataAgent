"""Codex 客户端与公开中转站之间的窄范围线协议兼容层。

这里只处理已经确认会被公开 relay 拒绝的 Codex 私有消息字段；请求在
数据库中的原始 JSON 仍由上层保存。该模块不改写响应、不决定会话归属，
也不把 API key 当作轨迹 ID。
"""

from __future__ import annotations

import json
from typing import Any


def responses_wire_body(body: dict[str, Any], raw: bytes) -> bytes:
    """生成转发给上游的 Responses 请求字节。

    输入是已解析的原始请求和原始 wire bytes；输出通常原样返回，只有当
    ``input`` 中的普通 ``message`` 携带
    ``internal_chat_message_metadata_passthrough`` 且包含
    ``content_item_kinds`` 时，才复制请求并删除这个私有字段。函数不会
    修改调用者传入的 ``body``，因此捕获和重建仍可使用完整原文。reasoning
    与工具项上的同名元数据会保留，以免破坏加密内容回放。
    """
    items = body.get("input")
    field = "internal_chat_message_metadata_passthrough"
    def removable(item: Any) -> bool:
        """输入一个 JSON item，输出是否符合已知私有注解删除条件；只读。"""
        # The public relay rejected this Codex-only annotation on ordinary
        # message items. Reasoning and tool items can carry authenticated
        # metadata; retaining it is required for encrypted-content replay.
        return (
            isinstance(item, dict)
            and item.get("type") == "message"
            and isinstance(item.get(field), dict)
            and "content_item_kinds" in item[field]
        )

    if not isinstance(items, list) or not any(removable(item) for item in items):
        return raw
    outgoing = {**body, "input": [
        {key: value for key, value in item.items() if key != field} if removable(item) else item
        for item in items
    ]}
    return json.dumps(outgoing, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
