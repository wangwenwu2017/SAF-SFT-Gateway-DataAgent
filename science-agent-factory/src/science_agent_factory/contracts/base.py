"""基础模型、路径校验与时间函数。

模块用途：集中定义基础模型、路径校验与时间函数，不承担工作流编排。
主要接口：PathIdentifier、utc_now、StrictModel。
输入：配置字段、制品数据或状态字典。
输出：校验后的模型、规范化值或异常。
副作用与边界：不读写制品或状态文件，不调用 Agent 或启动外部进程。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
)

_PATH_IDENTIFIER_PATTERN = r"^[A-Za-z0-9._-]+$"


def _validate_path_identifier(value: str) -> str:
    """保留既有标识符字符集，同时拒绝会改变路径语义的两个组件。"""

    if value in {".", ".."}:
        raise ValueError("path identifier cannot be '.' or '..'")
    return value


PathIdentifier = Annotated[
    str,
    Field(pattern=_PATH_IDENTIFIER_PATTERN),
    AfterValidator(_validate_path_identifier),
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("path must stay inside the Agent workspace")
    return path.as_posix()


def _safe_public_contract_ref(value: str) -> str:
    """校验 ``相对路径#片段`` 形式的公开契约引用。"""

    path_text, separator, fragment = value.partition("#")
    path = _safe_relative(path_text)
    if any(part in {"private", "reference", "verifier"} for part in Path(path).parts):
        raise ValueError("public contract reference cannot name private material")
    if separator and not fragment:
        raise ValueError("public contract fragment cannot be empty")
    return f"{path}#{fragment}" if separator else path


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


__all__ = ["PathIdentifier", "StrictModel", "utc_now"]
