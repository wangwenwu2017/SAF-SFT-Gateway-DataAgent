"""探索报告、任务设计、能力映射。

模块用途：定义探索报告、构建内设计卡和能力映射，不承担独立设计阶段或工作流编排。
主要接口：ExplorationExperiment、DifficultyDimension、DifficultyDimensionScore、DifficultyAssessment、CapabilityRelation、CapabilityMappingEntry、CapabilityMapping、PlannedClaimEvidence、PlannedAdversarialShortcut、TaskDesignCard、ExplorationReport。
输入：配置字段、制品数据或状态字典。
输出：校验后的模型、规范化值或异常。
副作用与边界：不读写制品或状态文件，不调用 Agent 或启动外部进程。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from .base import PathIdentifier, StrictModel, _safe_relative


class ExplorationExperiment(StrictModel):
    hypothesis: str = Field(min_length=1)
    commands: list[str] = Field(min_length=1)
    outcome: Literal["confirmed", "refuted", "inconclusive"]
    observation: str = Field(min_length=1)
    evidence_paths: list[str] = Field(min_length=1)
    design_implication: str = Field(min_length=1)

    @field_validator("commands")
    @classmethod
    def nonempty_commands(cls, value: list[str]) -> list[str]:
        if any(not command.strip() for command in value):
            raise ValueError("exploration commands cannot be empty")
        return value

    @field_validator("evidence_paths")
    @classmethod
    def safe_exploration_evidence(cls, value: list[str]) -> list[str]:
        return [_safe_relative(item) for item in value]


class DifficultyDimension(StrEnum):
    GENERALITY = "generality"
    ALGORITHMIC_DEPTH = "algorithmic_depth"
    IMPLEMENTATION_SCOPE = "implementation_scope"
    EDGE_CASE_COVERAGE = "edge_case_coverage"
    ENVIRONMENT_TOOLING = "environment_tooling"


class DifficultyDimensionScore(StrictModel):
    dimension: DifficultyDimension
    seed_score: int = Field(ge=0, le=4)
    candidate_score: int = Field(ge=0, le=4)
    evidence: str = Field(min_length=1)


class DifficultyAssessment(StrictModel):
    policy: Literal["preserve"] = "preserve"
    dimensions: list[DifficultyDimensionScore]
    hidden_generalization: bool
    candidate_requires_executable_code: bool
    verdict: Literal["preserved", "degraded", "unknown"]
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def complete_dimensions(self) -> DifficultyAssessment:
        names = [item.dimension for item in self.dimensions]
        required = set(DifficultyDimension)
        if len(names) != len(set(names)) or set(names) != required:
            raise ValueError(
                "difficulty assessment requires each difficulty dimension exactly once"
            )
        seed_total = sum(item.seed_score for item in self.dimensions)
        candidate_total = sum(item.candidate_score for item in self.dimensions)
        if self.verdict == "preserved" and candidate_total < seed_total:
            raise ValueError("preserved difficulty cannot have a lower candidate score total")
        return self

    @property
    def relation(self) -> Literal["degraded", "peer", "harder", "unknown"]:
        """把结构化结论与维度总分归一为导出使用的难度关系。"""

        if self.verdict == "unknown":
            return "unknown"
        seed_total = sum(item.seed_score for item in self.dimensions)
        candidate_total = sum(item.candidate_score for item in self.dimensions)
        if self.verdict == "degraded" or candidate_total < seed_total:
            return "degraded"
        return "harder" if candidate_total > seed_total else "peer"


class CapabilityRelation(StrEnum):
    PRESERVED = "preserved"
    ANALOGOUS = "analogous"
    DROPPED = "dropped"
    ADDED = "added"


class CapabilityMappingEntry(StrictModel):
    mapping_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    seed_capability: str | None = None
    candidate_capability: str | None = None
    relation: CapabilityRelation
    core: bool
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_relation(self) -> CapabilityMappingEntry:
        if self.relation == CapabilityRelation.ADDED:
            if self.seed_capability is not None or not self.candidate_capability:
                raise ValueError("added capability requires only candidate_capability")
        elif not self.seed_capability:
            raise ValueError("non-added capability requires seed_capability")
        if (
            self.relation in {CapabilityRelation.PRESERVED, CapabilityRelation.ANALOGOUS}
            and not self.candidate_capability
        ):
            raise ValueError("preserved/analogous capability requires a candidate mapping")
        if self.relation == CapabilityRelation.DROPPED and self.candidate_capability is not None:
            raise ValueError("dropped capability cannot name a candidate capability")
        return self


class CapabilityMapping(StrictModel):
    schema_version: Literal["saf.capability-mapping.v1"] = "saf.capability-mapping.v1"
    entries: list[CapabilityMappingEntry] = Field(min_length=1)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_mapping_ids(self) -> CapabilityMapping:
        identifiers = [item.mapping_id for item in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("capability mapping IDs must be unique")
        return self


class PlannedClaimEvidence(StrictModel):
    """构建内设计对一项验收关键声明及其计划证据的约束。"""

    claim_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    public_requirement: str = Field(min_length=1)
    planned_verification: str = Field(min_length=1)
    representative_invalid_solution: str = Field(min_length=1)
    rejection_evidence: str = Field(min_length=1)


class PlannedAdversarialShortcut(StrictModel):
    """构建内设计记录的代表性低成本捷径及其拒绝计划。"""

    shortcut_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    description: str = Field(min_length=1)
    exploited_information: str = Field(min_length=1)
    rejection_plan: str = Field(min_length=1)


class TaskDesignCard(StrictModel):
    """Builder 在构建调用内形成、随任务元数据交付的结构化设计计划。"""

    schema_version: Literal["saf.task-design-card.v1"] = "saf.task-design-card.v1"
    task_id: PathIdentifier
    seed_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    substantive_changes: list[str] = Field(min_length=1)
    seed_shortest_solution: list[str] = Field(min_length=1)
    candidate_shortest_solution: list[str] = Field(min_length=1)
    capability_mapping: CapabilityMapping
    difficulty_assessment: DifficultyAssessment
    acceptance_claims: list[PlannedClaimEvidence] = Field(min_length=1)
    adversarial_shortcuts: list[PlannedAdversarialShortcut] = Field(min_length=1)
    rejected_superficial_ideas: list[str] = Field(min_length=1)
    implementation_risks: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_design_identifiers(self) -> TaskDesignCard:
        claim_ids = [item.claim_id for item in self.acceptance_claims]
        shortcut_ids = [item.shortcut_id for item in self.adversarial_shortcuts]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("design acceptance claim IDs must be unique")
        if len(shortcut_ids) != len(set(shortcut_ids)):
            raise ValueError("design shortcut IDs must be unique")
        return self


class ExplorationReport(StrictModel):
    schema_version: Literal["saf.exploration.v1"] = "saf.exploration.v1"
    seed_id: str
    seed_reproduction: ExplorationExperiment
    experiments: list[ExplorationExperiment] = Field(default_factory=list)
    selected_insight: str = Field(min_length=1)
    preserved_capabilities: list[str] = Field(min_length=1)
    substantive_changes: list[str] = Field(min_length=1)
    rejected_superficial_ideas: list[str] = Field(min_length=1)
    difficulty_assessment: DifficultyAssessment
    capability_mapping: CapabilityMapping


__all__ = [
    "CapabilityMapping",
    "CapabilityMappingEntry",
    "CapabilityRelation",
    "DifficultyAssessment",
    "DifficultyDimension",
    "DifficultyDimensionScore",
    "ExplorationExperiment",
    "ExplorationReport",
    "PlannedAdversarialShortcut",
    "PlannedClaimEvidence",
    "TaskDesignCard",
]
