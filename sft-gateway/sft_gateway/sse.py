"""有界 SSE 分帧器。输入任意切分 bytes，输出有序 data 帧。
保留帧 bytes，单帧超过配置上限抛 ValueError；不重排或判断模型终态。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SSEEvent:
    """一个含 data 字段的 SSE 帧：可选事件名、合并后的 data 文本和原始字节。"""

    event: str | None
    data: str
    raw: bytes

    def json(self) -> dict[str, Any] | None:
        """将 data 解码为 JSON 对象；非 JSON、[DONE] 和数组等均返回 None。"""
        try:
            value = json.loads(self.data)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


class SSEParser:
    """有状态 SSE 分帧器；每个上游流应使用独立实例，调用方顺序喂入字节。"""

    def __init__(self, max_frame_bytes: int = 8 * 1024 * 1024) -> None:
        """建立空的未完成帧缓冲区，不产生外部副作用。"""
        self._buffer = b""
        self.max_frame_bytes = max_frame_bytes

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        """追加网络字节块，按接收顺序返回可识别的完整 data 帧。

        保留未闭合尾部供下次调用；注释/心跳等不含 data 的帧不会出现在输出中。
        返回空列表只表示当前尚无可交付事件，不能判断响应是否完成。
        """
        self._buffer += chunk
        events: list[SSEEvent] = []
        while True:
            boundary = self._find_boundary(self._buffer)
            if boundary is None:
                if len(self._buffer) > self.max_frame_bytes:
                    raise ValueError("SSE frame exceeds configured limit")
                break
            end, width = boundary
            if end + width > self.max_frame_bytes:
                raise ValueError("SSE frame exceeds configured limit")
            raw = self._buffer[: end + width]
            self._buffer = self._buffer[end + width :]
            event = self._parse(raw)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> list[SSEEvent]:
        """EOF 时尝试解析最后一个未带空行的尾帧，返回零或一个事件。

        这是对部分上游末尾缺少分隔符的兼容处理；调用方仍需检查终态事件。
        """
        if not self._buffer.strip():
            return []
        raw = self._buffer
        self._buffer = b""
        event = self._parse(raw)
        return [event] if event is not None else []

    @staticmethod
    def _find_boundary(value: bytes) -> tuple[int, int] | None:
        """返回最早匹配的空行分隔符起点和长度；未找到则返回 None。"""
        # SSE permits LF, CRLF, and CR line endings.  Proxies in front of
        # model gateways occasionally normalize to CR-only, so treating only
        # LF would hold the complete response until EOF.
        candidates = [
            (value.find(b"\r\n\r\n"), 4),
            (value.find(b"\n\n"), 2),
            (value.find(b"\r\r"), 2),
        ]
        candidates = [(index, width) for index, width in candidates if index >= 0]
        return min(candidates, default=None, key=lambda item: item[0])

    @staticmethod
    def _parse(raw: bytes) -> SSEEvent | None:
        """解析单帧的 event/data 字段；无 data 返回 None，其他字段忽略。

        多个 data 行用换行连接；无效 UTF-8 仅在文本投影中替换，raw 仍保留。
        """
        text = raw.decode("utf-8", errors="replace")
        event_name: str | None = None
        data_lines: list[str] = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
        if not data_lines:
            return None
        return SSEEvent(event_name, "\n".join(data_lines), raw)
