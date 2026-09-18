"""共享 JSON 制品写入原语。

模块用途：统一工作流、批次和导出器写入可读 JSON 文件的编码、模型序列化和目录创建行为。
主要接口：``write_json``。
输入：目标路径和字典、列表或 Pydantic 模型。
输出：带 UTF-8 编码和尾换行的 JSON 文件。
副作用与边界：创建目标父目录并覆盖精确目标文件；原子替换单个制品，不负责状态锁或路径安全验证。
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def write_json(path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> None:
    """以项目统一格式写入 JSON 制品。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = ["write_json"]
