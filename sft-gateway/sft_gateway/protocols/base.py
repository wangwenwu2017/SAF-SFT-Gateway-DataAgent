"""协议适配器的最小接口定义。

本模块只声明网关需要的适配能力，不执行网络请求，也不负责保存轨迹。
一个适配器接收已经解析的 JSON 映射，输出响应 ID、请求上下文、模型目标
以及可供轨迹索引使用的消息列表。具体协议（Chat Completions、Responses）
分别在同目录的实现模块中完成。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol as TypingProtocol

from ..gateway.detection import Protocol


class ProtocolAdapter(TypingProtocol):
    """协议适配器的静态接口。

    输入均为单次请求或响应的 JSON 对象；输出是规范化后的字符串、字典
    列表，或 ``None``。实现不得假定请求一定包含完整历史，因为 Responses
    continuation 可能只提交增量输入。
    """

    protocol: Protocol

    def response_id(self, value: Mapping[str, Any] | None) -> str | None:
        """只读提取响应 ID；输入可为空，缺少字符串 ID 时输出 None。"""
        ...

    def previous_response_id(self, request: Mapping[str, Any]) -> str | None:
        """只读提取请求声明的父响应 ID；没有链接时输出 None。"""
        ...

    def extract_context(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        """输入请求 JSON，输出本次携带的有序上下文；不补造服务端历史。"""
        ...

    def extract_target(self, response: Mapping[str, Any]) -> list[dict[str, Any]]:
        """输入响应 JSON，输出本次模型目标 item 列表；不判断任务完成状态。"""
        ...

    def to_messages(self, request: Mapping[str, Any], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """输入请求及目标 items，输出消息投影；不修改输入或执行 I/O。"""
        ...
