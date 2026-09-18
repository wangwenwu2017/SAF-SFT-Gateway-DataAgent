"""受控文件路径判断、符号链接防护和复制/重置原语。

模块用途：为制品导入、workspace 投影、VM staging 和导出提供一致的文件安全操作。
主要接口：``reject_symlinks``、``remove_path``、``copy_artifact``、
``reset_directory``。
输入：源路径、目标路径、允许根目录和显式 replace 标志。
输出：布尔作用域结果或完成后的文件/目录树。
副作用与边界：可删除精确目标、递归复制或重建目录；默认拒绝覆盖，并始终拒绝源树中的符号链接。
"""

from __future__ import annotations

import shutil
from pathlib import Path


def reject_symlinks(source: Path) -> None:
    """Reject a file tree that could dereference content outside its declared root."""

    if source.is_symlink():
        raise ValueError(f"artifact is a symlink: {source}")
    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"artifact contains a symlink: {path}")


def remove_path(path: Path) -> None:
    """Remove exactly one filesystem entry without following symlinks."""

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def copy_artifact(source: Path, destination: Path, *, replace: bool = False) -> None:
    """Copy a file or directory after rejecting symlinks in the source tree."""

    if not source.exists():
        raise FileNotFoundError(source)
    reject_symlinks(source)
    if destination.exists() or destination.is_symlink():
        if not replace:
            raise FileExistsError(destination)
        remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def reset_directory(path: Path) -> Path:
    """Create an empty directory, replacing only the exact requested path."""

    if path.exists() or path.is_symlink():
        remove_path(path)
    path.mkdir(parents=True)
    return path
