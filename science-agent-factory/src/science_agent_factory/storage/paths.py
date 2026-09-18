"""受限导入和制品路径解析工具。

模块用途：统一批次导入清单的根目录约束，避免不同控制器各自实现略有差异的路径检查。
主要接口：``resolve_seed_source``。
输入：声明根目录、清单中的路径字符串和错误上下文。
输出：位于声明根目录内且不含符号链接的目录路径。
副作用与边界：只读取路径并执行确定性检查，不创建、复制或删除文件。
"""

from pathlib import Path

from .files import reject_symlinks


def resolve_seed_source(seed_root: Path, value: str) -> Path:
    """解析导入清单中的 Seed 目录并拒绝越界或符号链接。"""

    root = seed_root.resolve()
    declared = Path(value)
    if not value.strip() or ".." in declared.parts:
        raise ValueError(f"unsafe SeedBundle path in import manifest: {value}")
    target = declared.resolve() if declared.is_absolute() else (root / declared).resolve()
    if not target.is_relative_to(root) or not target.is_dir():
        raise ValueError(f"SeedBundle path escapes import root: {value}")
    reject_symlinks(target)
    return target


__all__ = ["resolve_seed_source"]
