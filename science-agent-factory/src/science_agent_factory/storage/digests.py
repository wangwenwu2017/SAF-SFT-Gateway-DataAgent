"""公开制品的稳定摘要工具。

模块用途：集中实现普通文件和无符号链接目录树的 SHA-256 摘要，供缓存、验证和工作流状态复用。
主要接口：``artifact_digest``、``tree_digest``。
输入：文件或目录路径。
输出：十六进制 SHA-256 字符串。
副作用与边界：只读取路径；遇到符号链接立即失败，不解析目录外内容。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .files import reject_symlinks


def artifact_digest(path: Path) -> str:
    """计算普通文件或目录树的稳定摘要。"""

    reject_symlinks(path)
    root = path.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_file() and not root.is_dir():
        raise ValueError(f"unsupported artifact type: {root}")
    digest = hashlib.sha256()
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for item in paths:
        if item.is_symlink():
            raise ValueError(f"cannot digest symlinked artifact: {item}")
        if not item.is_file():
            continue
        relative = item.name if root.is_file() else item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def tree_digest(path: Path) -> str:
    """计算目录树摘要；目录必须存在且不含符号链接。"""

    reject_symlinks(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_dir():
        raise NotADirectoryError(path)
    return artifact_digest(path)


__all__ = ["artifact_digest", "tree_digest"]
