"""通用 Seed 输入合同。

模块用途：通用 Seed 输入合同。
主要接口：SeedBundle、SeedProvenance。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：validate_files 只读检查路径；不运行任务。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field

from .base import PathIdentifier, StrictModel


class SeedProvenance(StrictModel):
    """上游 Seed 的可审计来源，不保存 solution 正文。"""

    source: str
    source_version: str
    source_task_id: str
    source_uri: str | None = None
    archive_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    license: str | None = None


class SeedBundle(StrictModel):
    """支持私有 solution 或成功轨迹的标准输入清单。"""

    schema_version: Literal["saf.seed.v2"] = "saf.seed.v2"
    seed_id: PathIdentifier
    task_card: str = "task_card.json"
    successful_trajectory: str | None = "trajectory.json"
    solution_path: str | None = None
    environment_dir: str | None = None
    input_dir: str | None = "input"
    software_dir: str | None = "software"
    provenance: SeedProvenance | None = None
    notes: str = ""

    def validate_files(self, root: Path) -> None:
        resolved_root = root.resolve()
        if self.successful_trajectory is None and self.solution_path is None:
            raise ValueError("seed bundle requires a solution or successful trajectory")
        self._require_path(resolved_root, "task_card", self.task_card, "file")
        if self.successful_trajectory is not None:
            self._require_path(
                resolved_root,
                "successful_trajectory",
                self.successful_trajectory,
                "file",
            )
        if self.solution_path is not None:
            self._require_path(resolved_root, "solution_path", self.solution_path, "either")
        for label, relative in (
            ("environment_dir", self.environment_dir),
            ("input_dir", self.input_dir),
            ("software_dir", self.software_dir),
        ):
            if relative is not None:
                self._require_path(resolved_root, label, relative, "directory")

    @staticmethod
    def _require_path(root: Path, label: str, relative: str, expected: str) -> Path:
        declared = Path(relative)
        if declared.is_absolute() or not declared.parts or ".." in declared.parts:
            raise ValueError(f"{label} does not stay inside seed bundle: {relative}")
        cursor = root
        for part in declared.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"{label} contains a symlink: {relative}")
        path = (root / declared).resolve()
        valid_type = {
            "file": path.is_file(),
            "directory": path.is_dir(),
            "either": path.is_file() or path.is_dir(),
        }[expected]
        if not path.is_relative_to(root) or not valid_type:
            raise ValueError(
                f"{label} does not resolve to a {expected} inside seed bundle: {relative}"
            )
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_symlink():
                    raise ValueError(f"{label} contains a symlink: {child.relative_to(root)}")
        return path
