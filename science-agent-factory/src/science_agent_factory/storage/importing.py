"""Benchmark Seed 导入器共享的只读 Git、摘要辅助函数。

模块用途：集中多个边界导入器完全相同的 Git、摘要和受管目录替换操作；不负责发现任务、解释
上游元数据、筛选任务或生成 SeedBundle 业务字段。
主要接口：``checkout_pinned_repository``、``tree_sha256`` 和
``replace_managed_import``。
输入：上游仓库路径与固定版本信息、待摘要的白名单路径、临时导入目录和目标目录。
输出：通过提交与工作区校验的仓库绝对路径、稳定 SHA-256 摘要和已替换的受管目录。
副作用与边界：仓库不存在时执行浅克隆；目录替换只接受带匹配导入清单的旧目标，使用同级备份回滚，
拒绝文件系统根、用户目录、项目根及与源码重叠的目标；不选择任务。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .files import reject_symlinks, remove_path


def _run_git(*arguments: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def checkout_pinned_repository(
    source: Path,
    *,
    version: str,
    repository: str,
    commit: str,
    label: str,
) -> Path:
    """读取或浅克隆仓库，并拒绝提交不符或存在本地修改的输入。"""

    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        _run_git("clone", "--depth", "1", "--branch", version, repository, str(source))
    actual_commit = _run_git("rev-parse", "HEAD", cwd=source)
    if actual_commit != commit:
        raise ValueError(f"{label} checkout commit mismatch: {actual_commit}")
    if _run_git("status", "--porcelain", cwd=source):
        raise ValueError(f"{label} checkout must be clean before import")
    return source.resolve()


def tree_sha256(paths: list[Path], root: Path) -> str:
    """按相对路径和文件内容计算白名单文件树的稳定摘要。"""

    digest = hashlib.sha256()
    for source in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        reject_symlinks(source)
        members = (
            [source] if source.is_file() else [item for item in source.rglob("*") if item.is_file()]
        )
        for member in sorted(members, key=lambda item: item.relative_to(root).as_posix()):
            relative = member.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            with member.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def replace_managed_import(
    staging: Path,
    destination: Path,
    *,
    source: Path,
    source_repository: str,
    source_version: str,
) -> None:
    """用可回滚重命名替换一个由同来源 SAF 清单管理的 Seed 根。"""

    staging = staging.resolve()
    destination = destination.resolve()
    source = source.resolve()
    filesystem_root = Path(destination.anchor).resolve()
    home = Path.home().resolve()
    cwd = Path.cwd().resolve()
    source_tree_root = Path(__file__).resolve().parents[3]
    protected = {filesystem_root, home, cwd}
    if (source_tree_root / "pyproject.toml").is_file():
        protected.add(source_tree_root)
    if destination in protected or cwd.is_relative_to(destination):
        raise ValueError(f"unsafe import destination: {destination}")
    if (
        destination == source
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError("import source and destination must not overlap")
    if staging.parent != destination.parent:
        raise ValueError("import staging and destination must share a parent directory")
    if not staging.is_dir() or staging.is_symlink():
        raise ValueError(f"import staging directory is invalid: {staging}")

    def load_manifest(root: Path, *, label: str) -> dict[str, Any]:
        manifest_path = root / "import_manifest.json"
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} has no valid import manifest: {root}") from exc
        if (
            value.get("schema_version") != "saf.seed-import.v1"
            or value.get("source_repository") != source_repository
            or value.get("source_version") != source_version
        ):
            raise ValueError(f"{label} belongs to a different source or version")
        return value

    load_manifest(staging, label="import staging directory")

    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError(f"import destination is not a managed directory: {destination}")
        load_manifest(destination, label="existing import destination")

        backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex[:8]}")
        os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except OSError as exc:
            try:
                os.replace(backup, destination)
            except OSError as rollback_exc:
                raise RuntimeError(
                    f"import replacement and rollback both failed: {rollback_exc}"
                ) from exc
            raise
        remove_path(backup)
        return
    os.replace(staging, destination)
