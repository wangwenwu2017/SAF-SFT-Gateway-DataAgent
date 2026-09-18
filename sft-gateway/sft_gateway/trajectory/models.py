"""轨迹索引和导出使用的数据模型。

这些 dataclass 只承载已完成请求、响应及其链接元数据，不负责网络 I/O、
重试或持久化。``ConversationIndex`` 在加入节点时会深拷贝，防止后续请求
修改原始对象影响已经形成的轨迹快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..gateway.detection import Protocol


@dataclass
class CompletedCompletion:
    """一轮已经完成的请求/响应记录。

    ``request``/``response`` 是原始 JSON，``context``/``target`` 是协议适配
    后的有序快照；索引不会重新生成 target。``parent_request_id``、
    ``link_method`` 和 ``prefix_length`` 解释父节点的推断来源。
    输入字段由捕获导出层构造，实例输出给 ConversationIndex。dataclass 本身
    不复制嵌套字典、不校验完成状态、不执行 I/O；复制和有效性判断由调用方负责。
    """
    request_id: str
    run_id: str
    protocol: Protocol
    model: str | None
    request: dict[str, Any]
    response: dict[str, Any]
    response_id: str | None
    previous_response_id: str | None
    context: list[dict[str, Any]]
    target: list[dict[str, Any]]
    fingerprint: str
    parent_request_id: str | None = None
    link_method: str = "root"
    prefix_length: int = 0
    # Native Codex thread/session identity, when available.  It is deliberately
    # separate from run_id: one capture run may contain several harness tasks.
    session_key: str | None = None
    capture_status: str | None = None
    recovered_from_events: bool = False
    eligibility_reasons: list[str] = field(default_factory=list)


@dataclass
class Trajectory:
    """一条从根到叶的独立轨迹。

    ``turns`` 按请求发生的逻辑顺序排列；``break_reason`` 表示链接失败或
    前缀歧义等原因。它是导出层的输入，不代表任务一定成功。
    由索引返回并交给导出器；构造本身不修改 turns，也不验证全历史是否覆盖
    每个请求实际使用的 instructions、tools 或不可见的服务端上下文。
    """
    trajectory_id: str
    run_id: str
    protocol: Protocol
    model: str | None
    turns: list[CompletedCompletion] = field(default_factory=list)
    break_reason: str | None = None
