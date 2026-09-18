"""网关环境配置的只读数据结构。

输入为环境变量及可选构造覆盖项，输出 GatewayConfig。这里不读取 .env 文件、
不创建目录或数据库；URL 结构校验由应用层负责，认证 key 缺省值为 None。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GatewayConfig:
    """不可变网关配置。输入环境解析值或构造参数，输出应用所需属性。
    key 字段不出现在 repr 中；配置对象仍含明文 key，不应序列化到日志。"""

    upstream_base_url: str
    upstream_api_key: str | None = field(repr=False)
    database_path: Path
    raw_archive_path: Path | None
    admin_api_key: str | None = field(default=None, repr=False)
    gateway_api_key: str | None = field(default=None, repr=False)
    request_timeout_seconds: float = 900.0
    strip_codex_internal_metadata: bool = False

    instance_id: str | None = None
    max_request_bytes: int = 64 * 1024 * 1024
    max_response_bytes: int = 64 * 1024 * 1024
    max_stream_bytes: int = 256 * 1024 * 1024
    max_frame_bytes: int = 8 * 1024 * 1024
    max_concurrent_requests: int = 32

    @classmethod
    def from_env(cls, *, upstream: str | None = None, db: str | None = None) -> "GatewayConfig":
        """输入可选 upstream/db 覆盖值，返回环境配置。
        不读取 .env 文件，不进行磁盘或网络 I/O；无效数字抛 ValueError，安全约束由 make_app 校验。"""
        raw_archive = os.getenv("SFT_RAW_ARCHIVE")
        return cls(
            upstream_base_url=(upstream or os.getenv("SFT_UPSTREAM_URL", "http://127.0.0.1:4000")).rstrip("/"),
            upstream_api_key=os.getenv("SFT_UPSTREAM_API_KEY") or None,
            database_path=Path(db or os.getenv("SFT_DB", "data/gateway.db")),
            raw_archive_path=Path(raw_archive) if raw_archive else None,
            admin_api_key=os.getenv("SFT_ADMIN_API_KEY") or None,
            gateway_api_key=os.getenv("SFT_GATEWAY_API_KEY") or None,
            request_timeout_seconds=float(os.getenv("SFT_REQUEST_TIMEOUT", "900")),
            instance_id=os.getenv("SFT_INSTANCE_ID") or None,
            max_request_bytes=int(os.getenv("SFT_MAX_REQUEST_BYTES", str(64 * 1024 * 1024))),
            max_response_bytes=int(os.getenv("SFT_MAX_RESPONSE_BYTES", str(64 * 1024 * 1024))),
            max_stream_bytes=int(os.getenv("SFT_MAX_STREAM_BYTES", str(256 * 1024 * 1024))),
            max_frame_bytes=int(os.getenv("SFT_MAX_FRAME_BYTES", str(8 * 1024 * 1024))),
            max_concurrent_requests=int(os.getenv("SFT_MAX_CONCURRENT_REQUESTS", "32")),
            strip_codex_internal_metadata=os.getenv("SFT_STRIP_CODEX_INTERNAL_METADATA", "0").lower() in {"1", "true", "yes"},
        )
