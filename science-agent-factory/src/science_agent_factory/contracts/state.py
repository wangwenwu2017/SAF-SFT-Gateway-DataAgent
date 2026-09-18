"""统一 Harbor 任务与固定槽位批次的持久状态。

模块用途：候选设计卡随构建元数据归档，不设独立设计状态；统一 Harbor 任务与固定槽位批次的持久状态。
主要接口：RolloutResult、AttemptState、CandidateState、JobState、BatchState。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：模型不执行 I/O；状态存储负责原子提交。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .base import PathIdentifier, StrictModel, utc_now
from .enums import BatchStatus, CandidateStage, FailureKind, JobStatus, WorkflowMode


class RolloutResult(StrictModel):
    schema_version: Literal["saf.rollout.v1"] = "saf.rollout.v1"
    outcome: Literal["completed", "timeout", "error", "cancelled"]
    return_code: int | None = None
    trajectory_path: str | None = None
    trial_result_path: str | None = None
    final_response: str = ""
    agent: str | None = None
    model: str | None = None
    verification_executed: bool = False
    rewards: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        reward = (self.rewards or {}).get("reward")
        return (
            self.outcome == "completed"
            and self.verification_executed
            and type(reward) in {int, float}
            and reward == 1
        )


class AttemptState(StrictModel):
    attempt_id: PathIdentifier
    revision: int
    result_dir: str
    result: RolloutResult
    accepted: bool = False
    reviewed: bool = False
    review_path: str | None = None


class CandidateState(StrictModel):
    candidate_id: PathIdentifier
    stage: CandidateStage = CandidateStage.PENDING
    revision: int = 0
    contract_repairs: int = 0
    semantic_revisions: int = 0
    distiller_revision: int = 0
    builder_thread_id: str | None = None
    distiller_thread_id: str | None = None
    task_dir: str | None = None
    metadata_dir: str | None = None
    task_sha256: str | None = None
    strategy_card_path: str | None = None
    preflight_review: str | None = None
    final_review: str | None = None
    attempts: list[AttemptState] = Field(default_factory=list)
    last_error: str | None = None
    failure_kind: FailureKind | None = None


class JobState(StrictModel):
    schema_version: Literal["saf.job.v4"] = "saf.job.v4"
    job_id: PathIdentifier
    seed_id: PathIdentifier
    seed_dir: str
    workflow_mode: WorkflowMode
    config_sha256: str
    config_base_dir: str
    settings_sha256: str
    status: JobStatus = JobStatus.CREATED
    candidates: list[CandidateState]
    active_step: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None

    @model_validator(mode="after")
    def unique_candidates(self):
        ids = [c.candidate_id for c in self.candidates]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be nonempty and unique")
        return self


class BatchTaskState(StrictModel):
    seed_id: PathIdentifier
    seed_dir: str
    job_id: PathIdentifier
    generation_resumes: int = 0
    status: Literal["pending", "generating", "ready", "collecting", "complete", "failed"] = (
        "pending"
    )
    error: str | None = None


class BatchState(StrictModel):
    schema_version: Literal["saf.batch.v5"] = "saf.batch.v5"
    batch_id: PathIdentifier
    seed_root: str
    status: BatchStatus = BatchStatus.CREATED
    tasks: list[BatchTaskState]
    config_sha256: str
    config_base_dir: str
    settings_sha256: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None
