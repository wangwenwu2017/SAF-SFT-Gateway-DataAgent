"""捕获范围、认证头和敏感头的辅助函数。

这些函数解析值并登记进程内 run 元数据，不创建数据库记录。run/session 的选择把
请求归入同一条采集范围；计费 key 只用于转发认证，不能被当作会话 ID。
"""

from __future__ import annotations

import re
import secrets
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_run_id(value: str) -> str:
    """去除空白并验证采集范围标识符。

    输入：字符串；输出：规范化 ID；非法格式抛 ValueError。无 I/O，不检查
    租户所有权；非字符串调用不受此函数保护，调用方应先做类型校验。"""
    value = value.strip()
    if not _ID.fullmatch(value):
        raise ValueError("run_id must start with a letter or number and contain only letters, numbers, '.', '_' or '-'")
    return value


def new_run_id() -> str:
    """生成随机采集标识符。

    输入：无；输出：run_ 加随机十六进制字符串。消耗系统随机数，不登记 ID、
    不创建数据库记录；唯一性依赖随机值而非持久化约束。"""
    return f"run_{secrets.token_hex(12)}"


@dataclass
class RunRegistry:
    """进程内的 run 元数据索引，不负责持久化轨迹或响应正文。"""
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def ensure(self, run_id: str, *, explicit: bool = False) -> str:
        """在本进程 registry 登记 run。

        输入：run_id 及 explicit 标志；输出：规范化 ID。修改 self.runs，但不会
        覆盖已有元数据或写 SQLite；非法 ID 抛 ValueError，不跨 worker 同步。"""
        run_id = validate_run_id(run_id)
        self.runs.setdefault(run_id, {"run_id": run_id, "explicit": explicit})
        return run_id


def resolve_run_id(
    headers: Mapping[str, str],
    query: Mapping[str, str],
    body: Mapping[str, Any],
    registry: RunRegistry,
    *,
    path_run_id: str | None = None,
) -> str:
    """解析捕获范围，不把计费凭据误当作范围标识。

    输入是 headers、query、body、进程内 registry，以及可选的路径 run ID；输出
    是已登记的 run ID。原生 Codex 通常把 thread/session 放在 headers 而不是
    body 中，因此显式路径优先，其次是代理 session 头、原生 thread/session
    头、Codex turn metadata、query/body，全部缺失时归入 ``unassigned``。
    第一个非空候选若格式非法会直接报错，不会静默尝试下一候选。全部缺失的
    多个客户端会共用 unassigned，因此它不提供会话隔离。函数不读取上游 key。
    """
    lower = {str(k).lower(): str(v) for k, v in headers.items()}
    metadata: dict[str, Any] = {}
    raw_turn_metadata = lower.get("x-codex-turn-metadata")
    if raw_turn_metadata:
        try:
            parsed = json.loads(raw_turn_metadata)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed

    candidates = [
        path_run_id,
        lower.get("x-session-id"),
        lower.get("x_session_id"),
        lower.get("x-proxy-session-id"),
        lower.get("proxy-x-session-id"),
        lower.get("proxy_x_session_id"),
        lower.get("thread-id"),
        lower.get("thread_id"),
        lower.get("session-id"),
        lower.get("session_id"),
        metadata.get("thread_id") if isinstance(metadata.get("thread_id"), str) else None,
        metadata.get("session_id") if isinstance(metadata.get("session_id"), str) else None,
        query.get("session_id"),
        body.get("_proxy_session_id") if isinstance(body.get("_proxy_session_id"), str) else None,
    ]
    for candidate in candidates:
        if candidate:
            return registry.ensure(candidate, explicit=True)
    return registry.ensure("unassigned", explicit=False)


def extract_native_session_id(headers: Mapping[str, str]) -> str | None:
    """提取 harness 自报的会话作用域。

    输入：headers Mapping；输出：thread/session 字符串或 None。优先 thread
    头、metadata.thread_id，再 session 头及 metadata.session_id；不修改输入。
    普通 header 值去首尾空白，metadata 中的值当前不做 trim/ID 格式校验。
    这些 ID 用于拼接隔离而非身份认证，也不能证明调用者拥有该会话。"""
    lower = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    metadata: dict[str, Any] = {}
    raw = lower.get("x-codex-turn-metadata")
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed
    candidates = (
        lower.get("thread-id"), lower.get("thread_id"),
        metadata.get("thread_id") if isinstance(metadata.get("thread_id"), str) else None,
        lower.get("session-id"), lower.get("session_id"),
        metadata.get("session_id") if isinstance(metadata.get("session_id"), str) else None,
    )
    return next((candidate for candidate in candidates if candidate), None)


def extract_auth(headers: Mapping[str, str], configured_key: str | None) -> str | None:
    """选择转发给上游的认证字符串。

    输入：headers 和可选 configured_key；输出：认证字符串或 None。配置 key
    优先，并补 Bearer 前缀；其次 Bearer 头，再 x-api-key/x-goog-api-key 原值。
    无 I/O、不决定采集范围；API-key 头分支不补 Bearer，调用者需确保上游接受。"""
    if configured_key:
        return configured_key if configured_key.lower().startswith("bearer ") else f"Bearer {configured_key}"
    lower = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = lower.get("authorization")
    if auth and auth.lower().startswith("bearer ") and auth[7:].strip():
        return auth
    if lower.get("x-api-key"):
        return lower["x-api-key"]
    if lower.get("x-goog-api-key"):
        return lower["x-goog-api-key"]
    return None


def redacted_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """生成可落库的请求头副本。

    输入：headers Mapping；输出：小写头名字典，移除显式列出的认证和 cookie。
    无 I/O、不修改输入；不会识别其他自定义秘密头或清理正文中的敏感业务数据。"""
    secret = {
        "authorization", "proxy-authorization", "x-api-key", "x-goog-api-key",
        "x-admin-key", "x-gateway-key", "cookie", "set-cookie",
    }
    return {str(k).lower(): str(v) for k, v in headers.items() if str(k).lower() not in secret}
