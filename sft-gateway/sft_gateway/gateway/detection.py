"""请求协议识别。

本模块只根据 HTTP 路径和请求 JSON 的外形判断是 Responses 还是 Chat
Completions；它不发送请求，也不修改请求内容。输入是网关收到的 path、headers
和已解析的 body，输出是 :class:`Protocol`，无法判断时抛出 ``ValueError``。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class Protocol(str, Enum):
    """网关当前接受的两类模型 API 协议标识。"""

    RESPONSES = "responses"
    CHAT = "chat"


def detect_protocol(path: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Protocol:
    """根据请求外形选择协议适配器。

    输入：路径、headers 和已解析 JSON Mapping；输出：Protocol；无法判断抛
    ValueError。无 I/O、不改输入。路径优先，其次 input/instructions 或 messages；
    headers 暂未使用。可识别 /responses/compact，但这不意味着支持其训练投影，
    也不是允许转发端点的安全白名单或完整的 schema 验证。"""
    normalized = path.rstrip("/")
    if normalized.endswith("/responses") or normalized.endswith("/responses/compact"):
        return Protocol.RESPONSES
    if normalized.endswith("/chat/completions"):
        return Protocol.CHAT
    if "input" in body or "instructions" in body:
        return Protocol.RESPONSES
    if "messages" in body:
        return Protocol.CHAT
    raise ValueError(f"cannot detect protocol for {path}")
