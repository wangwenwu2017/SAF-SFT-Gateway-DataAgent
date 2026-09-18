"""请求级上下文指纹。

指纹用于在同一 run/protocol/model 范围内缩小前缀候选，不是安全哈希、
不是用户身份，也不是 token-level Polar prompt ID。这里只纳入会影响后续
请求语义的稳定字段；调用方仍需通过显式 previous_response_id 或精确前缀
比较确认父节点。

请求配置变动会让无显式链接的记录成为新候选组；本函数不判断这种变化是否
仍属于同一真实任务，也不验证一个最终 tools 字段能否描述历史上的全部调用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..gateway.detection import Protocol


def context_fingerprint(*, protocol: Protocol, model: str | None, request: dict[str, Any]) -> str:
    """返回稳定 SHA-256 指纹。

    输入为协议、模型和原始请求字典；输出为十六进制字符串。JSON 使用
    排序键和紧凑分隔符，函数不修改 request。
    """
    stable = {
        "protocol": protocol.value,
        "model": model,
        "instructions": request.get("instructions"),
        "tools": request.get("tools"),
        "tool_choice": request.get("tool_choice"),
        "text": request.get("text"),
        "response_format": request.get("response_format"),
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
