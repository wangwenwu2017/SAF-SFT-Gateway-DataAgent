"""审查结论与结构化反思。

模块用途：集中定义审查结论与结构化反思，不承担工作流编排。
主要接口：AuditIssue、ReflectionFailureClass、SolverSymptomCode、BuilderReflection、DistillerReflection、SolverReflection、AuditReport。
输入：配置字段、制品数据或状态字典。
输出：校验后的模型、规范化值或异常。
副作用与边界：不读写制品或状态文件，不调用 Agent 或启动外部进程。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from .base import StrictModel, _safe_public_contract_ref
from .design import CapabilityMapping, DifficultyAssessment


class AuditIssue(StrictModel):
    code: str = Field(pattern=r"^[A-Z0-9_]+$")
    severity: Literal["error", "warning"]
    evidence: str
    required_fix: str


class ReflectionFailureClass(StrEnum):
    CONTRACT = "contract"
    ENVIRONMENT = "environment"
    INTERFACE = "interface"
    CORRECTNESS = "correctness"
    ROBUSTNESS = "robustness"
    INCOMPLETE = "incomplete"
    EXECUTION = "execution"
    POLICY = "policy"


class SolverSymptomCode(StrEnum):
    OUTPUT_MISSING = "output_missing"
    EXIT_NONZERO = "exit_nonzero"
    SCHEMA_INVALID = "schema_invalid"
    CLAIM_FAILED = "claim_failed"
    ENVIRONMENT_CONTRACT_VIOLATION = "environment_contract_violation"
    INCOMPLETE_DELIVERY = "incomplete_delivery"


class BuilderReflection(StrictModel):
    schema_version: Literal["saf.builder-reflection.v1"] = "saf.builder-reflection.v1"
    source_phase: Literal[
        "build_delivery",
        "deterministic_preflight",
        "preflight",
        "final",
        "diagnosis",
    ]
    failure_class: ReflectionFailureClass
    issue_codes: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    required_actions: list[str] = Field(min_length=1)
    avoid_actions: list[str] = Field(min_length=1)
    retryable: bool

    @field_validator("issue_codes")
    @classmethod
    def valid_issue_codes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"[A-Z0-9_]+", item) is None for item in value
        ):
            raise ValueError("builder reflection issue codes must be unique stable codes")
        return value

    @model_validator(mode="after")
    def task_issues_are_not_contract_only(self) -> BuilderReflection:
        if self.failure_class == ReflectionFailureClass.CONTRACT and any(
            code.startswith("TASK_") for code in self.issue_codes
        ):
            raise ValueError("TASK_* issues are semantic and cannot use the contract repair budget")
        return self


class DistillerReflection(StrictModel):
    """Auditor 面向 Distiller 的公开、脱敏策略卡修订意见。"""

    schema_version: Literal["saf.distiller-reflection.v1"] = "saf.distiller-reflection.v1"
    source_revision: int = Field(ge=0)
    failure_class: ReflectionFailureClass
    issue_codes: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    public_contract_refs: list[str] = Field(min_length=1)
    required_actions: list[str] = Field(min_length=1)
    avoid_actions: list[str] = Field(min_length=1)
    disclosure_level: Literal["public_only"] = "public_only"

    @field_validator("issue_codes")
    @classmethod
    def valid_issue_codes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"[A-Z0-9_]+", item) is None for item in value
        ):
            raise ValueError("distiller reflection issue codes must be unique stable codes")
        return value

    @field_validator("public_contract_refs")
    @classmethod
    def safe_distiller_contract_refs(cls, value: list[str]) -> list[str]:
        normalized = [_safe_public_contract_ref(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("distiller public contract references must be unique")
        return normalized


class SolverReflection(StrictModel):
    schema_version: Literal["saf.solver-reflection.v1"] = "saf.solver-reflection.v1"
    source_attempt_id: str = Field(min_length=1)
    failure_class: ReflectionFailureClass
    symptom_codes: list[SolverSymptomCode] = Field(min_length=1)
    failed_claim_ids: list[str] = Field(default_factory=list)
    public_contract_refs: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    avoidance_advice: list[str] = Field(min_length=1)
    disclosure_level: Literal["public_only"] = "public_only"

    @field_validator("failed_claim_ids")
    @classmethod
    def unique_failed_claims(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("failed claim IDs must be unique")
        return value

    @field_validator("public_contract_refs")
    @classmethod
    def safe_solver_contract_refs(cls, value: list[str]) -> list[str]:
        normalized = [_safe_public_contract_ref(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("solver public contract references must be unique")
        return normalized


class AuditReport(StrictModel):
    schema_version: Literal["saf.audit.v1"] = "saf.audit.v1"
    phase: Literal["preflight", "final", "diagnosis"]
    decision: Literal["accept", "revise", "reject"]
    target: Literal["builder", "distiller", "solver", "none"]
    issues: list[AuditIssue] = Field(default_factory=list)
    reference_passed: bool | None = None
    empty_output_rejected: bool | None = None
    solver_output_passed: bool | None = None
    diagnosis_cause: Literal["infrastructure", "task", "solver", "unknown"] | None = None
    difficulty_assessment: DifficultyAssessment | None = None
    capability_mapping: CapabilityMapping | None = None
    summary: str
    builder_reflection: BuilderReflection | None = None
    distiller_reflection: DistillerReflection | None = None
    solver_reflection: SolverReflection | None = None

    @model_validator(mode="after")
    def coherent_decision(self) -> AuditReport:
        if self.phase == "diagnosis":
            if self.decision == "accept":
                raise ValueError("diagnosis cannot accept a rollout")
            if self.diagnosis_cause is None:
                raise ValueError(
                    "diagnosis requires a cause, including unknown when evidence is insufficient"
                )
            allowed_targets = (
                {"builder", "distiller"} if self.diagnosis_cause == "task" else {"solver"}
            )
            if self.decision == "revise" and self.target not in allowed_targets:
                raise ValueError("diagnosis revision target must match its evidenced cause")
        elif self.diagnosis_cause is not None:
            raise ValueError("diagnosis_cause is only valid for diagnosis reports")
        if self.decision == "accept" and (
            self.target != "none" or any(issue.severity == "error" for issue in self.issues)
        ):
            raise ValueError("accepted audit cannot target a revision or contain errors")
        if self.decision == "accept" and self.phase == "preflight":
            if self.difficulty_assessment is None:
                raise ValueError("preflight accept requires a difficulty assessment")
            if self.difficulty_assessment.verdict != "preserved":
                raise ValueError("preflight accept requires preserved difficulty")
            if any(issue.code == "TASK_DIFFICULTY_DEGRADED" for issue in self.issues):
                raise ValueError("difficulty degradation cannot be accepted as a warning")
        if self.decision == "revise" and self.target == "none":
            raise ValueError("revision audit must identify builder or solver")
        if self.decision == "revise" and self.target == "builder":
            if self.builder_reflection is None or any(
                item is not None for item in (self.distiller_reflection, self.solver_reflection)
            ):
                raise ValueError("builder revision requires only builder_reflection")
        elif self.decision == "revise" and self.target == "distiller":
            if self.distiller_reflection is None or any(
                item is not None for item in (self.builder_reflection, self.solver_reflection)
            ):
                raise ValueError("distiller revision requires only distiller_reflection")
        elif self.decision == "revise" and self.target == "solver":
            if self.solver_reflection is None or any(
                item is not None for item in (self.builder_reflection, self.distiller_reflection)
            ):
                raise ValueError("solver revision requires only solver_reflection")
        elif any(
            item is not None
            for item in (
                self.builder_reflection,
                self.distiller_reflection,
                self.solver_reflection,
            )
        ):
            raise ValueError("only revision audits can include reflection")
        return self


__all__ = [
    "AuditIssue",
    "AuditReport",
    "BuilderReflection",
    "DistillerReflection",
    "ReflectionFailureClass",
    "SolverReflection",
    "SolverSymptomCode",
]
