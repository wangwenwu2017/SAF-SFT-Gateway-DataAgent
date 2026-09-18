"""Harbor 任务交付、生成元数据与授权方法卡。

模块用途：Harbor 任务交付、生成元数据与授权方法卡。
主要接口：HarborBuilderDelivery、HarborTaskMetadata、HarborSelfTestReport、SolverStrategyCard、SolverGuidanceCard。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：只定义数据，不执行外部调用。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .base import PathIdentifier, StrictModel, _safe_public_contract_ref, _safe_relative


class HarborBuilderDelivery(StrictModel):
    """Builder 在两种工作流中直接交付的完整 Harbor Task 根与旁路元数据。"""

    schema_version: Literal["saf.harbor-builder-delivery.v1"] = "saf.harbor-builder-delivery.v1"
    task_id: PathIdentifier
    task_dir: str = "outputs/harbor-task"
    metadata_dir: str = "outputs/task-metadata"
    summary: str = Field(min_length=1)

    @field_validator("task_dir", "metadata_dir")
    @classmethod
    def safe_output_dirs(cls, value: str) -> str:
        return _safe_relative(value)


class HarborTaskMetadata(StrictModel):
    """不进入 Harbor Task 根的生成来源与自测索引。"""

    schema_version: Literal["saf.harbor-task-metadata.v1"] = "saf.harbor-task-metadata.v1"
    task_id: PathIdentifier
    seed_id: str = Field(min_length=1)
    environment_source: Literal["synthesized", "seed_copy"]
    seed_environment_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    exploration_report_path: str = "exploration_report.json"
    environment_report_path: str = "environment_report.json"
    self_test_report_path: str = "self_test.json"

    @field_validator("exploration_report_path", "environment_report_path", "self_test_report_path")
    @classmethod
    def safe_metadata_paths(cls, value: str) -> str:
        return _safe_relative(value)

    @model_validator(mode="after")
    def coherent_environment_source(self) -> HarborTaskMetadata:
        if self.environment_source == "seed_copy" and self.seed_environment_sha256 is None:
            raise ValueError("seed_copy metadata requires seed_environment_sha256")
        if self.environment_source == "synthesized" and self.seed_environment_sha256:
            raise ValueError("synthesized metadata cannot declare a Seed digest")
        return self


class HarborSelfTestReport(StrictModel):
    """Builder 对完整 Harbor Task 执行 Oracle 与负例验证的最小证据。"""

    schema_version: Literal["saf.harbor-self-test.v2"] = "saf.harbor-self-test.v2"
    task_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    oracle_result_path: str
    negative_result_path: str
    task_loadable: bool
    oracle_reward: float = Field(ge=0.0, le=1.0)
    negative_reward: float = Field(ge=0.0, le=1.0)
    commands: list[str] = Field(min_length=2)
    evidence_paths: list[str] = Field(min_length=1)
    cleaned: bool

    @field_validator("commands")
    @classmethod
    def nonempty_self_test_commands(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Harbor self-test commands cannot be empty")
        return value

    @field_validator("oracle_result_path", "negative_result_path")
    @classmethod
    def safe_result_path(cls, value: str) -> str:
        return _safe_relative(value)

    @field_validator("evidence_paths")
    @classmethod
    def safe_self_test_evidence(cls, value: list[str]) -> list[str]:
        return [_safe_relative(item) for item in value]

    @model_validator(mode="after")
    def passed_reference_and_negative(self) -> HarborSelfTestReport:
        if not self.task_loadable:
            raise ValueError("Harbor self-test did not load the task")
        if self.oracle_reward != 1.0 or self.negative_reward != 0.0:
            raise ValueError("Harbor self-test requires Oracle=1 and negative=0")
        if not self.cleaned:
            raise ValueError("Harbor self-test must clean its resources")
        return self


class SolverStrategyCard(StrictModel):
    """Distiller 的内部策略卡，保留私有知识来源以供门禁与审计使用。"""

    schema_version: Literal["saf.solver-strategy-card.v1"] = "saf.solver-strategy-card.v1"
    task_id: PathIdentifier
    seed_id: str = Field(min_length=1)
    knowledge_source: Literal["target_reference"] = "target_reference"
    objective: str = Field(min_length=1)
    task_difference: str = Field(min_length=1)
    method_steps: list[str] = Field(min_length=1)
    tool_patterns: list[str] = Field(min_length=1)
    decision_points: list[str] = Field(min_length=1)
    invariants: list[str] = Field(min_length=1)
    failure_modes: list[str] = Field(min_length=1)
    verification_strategy: list[str] = Field(min_length=1)
    public_contract_refs: list[str] = Field(min_length=1)
    excluded_instance_details: list[str] = Field(min_length=1)

    @field_validator("public_contract_refs")
    @classmethod
    def safe_strategy_refs(cls, value: list[str]) -> list[str]:
        normalized = [_safe_public_contract_ref(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("strategy public contract references must be unique")
        return normalized


class SolverGuidanceCard(StrictModel):
    """投影给 Solver 的自包含公开方法指导，不携带种子或内部比较信息。"""

    schema_version: Literal["saf.solver-guidance-card.v1"] = "saf.solver-guidance-card.v1"
    task_id: PathIdentifier
    objective: str = Field(min_length=1)
    method_steps: list[str] = Field(min_length=1)
    tool_patterns: list[str] = Field(min_length=1)
    decision_points: list[str] = Field(min_length=1)
    invariants: list[str] = Field(min_length=1)
    failure_modes: list[str] = Field(min_length=1)
    verification_strategy: list[str] = Field(min_length=1)
    public_contract_refs: list[str] = Field(min_length=1)

    @field_validator("public_contract_refs")
    @classmethod
    def safe_guidance_refs(cls, value: list[str]) -> list[str]:
        normalized = [_safe_public_contract_ref(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("guidance public contract references must be unique")
        return normalized

    @classmethod
    def from_strategy_card(cls, card: SolverStrategyCard) -> SolverGuidanceCard:
        """丢弃仅用于内部溯源的字段，生成 Solver 可见的最小方法上下文。"""
        return cls(
            task_id=card.task_id,
            objective=card.objective,
            method_steps=card.method_steps,
            tool_patterns=card.tool_patterns,
            decision_points=card.decision_points,
            invariants=card.invariants,
            failure_modes=card.failure_modes,
            verification_strategy=card.verification_strategy,
            public_contract_refs=card.public_contract_refs,
        )
