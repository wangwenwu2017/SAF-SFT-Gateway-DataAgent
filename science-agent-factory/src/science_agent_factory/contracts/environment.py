"""SDK 内部角色的环境清理证据。

模块用途：SDK 内部角色的环境清理证据。
主要接口：EnvironmentResource、EnvironmentRunReport。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：只定义数据，不执行外部调用。
"""

from typing import Literal

from pydantic import Field, field_validator

from .base import StrictModel, _safe_relative


class EnvironmentResource(StrictModel):
    kind: str
    identifier: str
    pid: int | None = Field(default=None, ge=1)
    pid_file: str | None = None

    @field_validator("pid_file")
    @classmethod
    def safe_pid_file(cls, value: str | None) -> str | None:
        return None if value is None else _safe_relative(value)


class EnvironmentRunReport(StrictModel):
    schema_version: Literal["saf.environment-run.v1"] = "saf.environment-run.v1"
    backend: str
    detected_inputs: list[str] = Field(default_factory=list)
    start_commands: list[str] = Field(default_factory=list)
    experiment_commands: list[str] = Field(default_factory=list)
    resources: list[EnvironmentResource] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)
    cleaned: bool
    cleanup_summary: str

    @field_validator("evidence_paths")
    @classmethod
    def safe_evidence_paths(cls, value: list[str]) -> list[str]:
        return [_safe_relative(item) for item in value]
