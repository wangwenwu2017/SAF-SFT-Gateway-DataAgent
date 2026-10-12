"""Harbor-only 任务生成、审查与轨迹归档。

模块用途：编排 Builder 构建和任务预审；仅任务生成跳过蒸馏，完整流程增加策略审核与轨迹归档。
主要接口：ScienceAgentFactory.run、generate_tasks、resume_generation、resume、status、job_dir。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：SDK 三角色使用独立暂存输入和会话标识；Harbor 执行全部求解和验证；单写者锁与原子 checkpoint 保证恢复。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from ..agents.harbor import HarborRunner, freeze_config, valid_atif
from ..agents.roles import AgentRuntime, AuditorAgent, DistillerAgent, TaskBuilderAgent
from ..agents.runtime import CodexRuntime, DeliveryContractError
from ..agents.seed_environment import SeedEnvironmentCache
from ..contracts.base import PathIdentifier
from ..contracts.deliveries import SolverGuidanceCard, SolverStrategyCard
from ..contracts.enums import AgentRole, CandidateStage, FailureKind, JobStatus, WorkflowMode
from ..contracts.models import SeedBundle
from ..contracts.reviews import AuditReport
from ..contracts.settings import FactorySettings
from ..contracts.state import AttemptState, CandidateState, JobState, RolloutResult
from ..storage.digests import artifact_digest
from ..storage.files import copy_artifact
from ..storage.json import write_json
from ..verification.harbor_task import harbor_task_fingerprint, validate_harbor_delivery
from .guidance import atif_boundary_violation, validate_strategy_card
from .state import JobStateStore
from .workspace import WorkspaceManager


def execution_settings_digest(settings: FactorySettings) -> str:
    payload = {
        "pipeline": settings.pipeline.model_dump(mode="json"),
        "codex": settings.codex.model_dump(mode="json"),
        "agents": {k.value: v.model_dump(mode="json") for k, v in settings.agents.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ScienceAgentFactory:
    def __init__(
        self,
        settings: FactorySettings,
        runtime: AgentRuntime | None = None,
        runner=None,
        config_base_dir: Path | None = None,
    ):
        self.settings = settings
        self._runtime = runtime
        self._runner = runner
        self._config_base_dir = config_base_dir
        self._save_lock = asyncio.Lock()

    def job_dir(self, job_id: str) -> Path:
        TypeAdapter(PathIdentifier).validate_python(job_id)
        root = (self.settings.storage_root / "jobs").resolve()
        path = (root / job_id).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("Job path escapes storage")
        return path

    async def run(self, seed_dir: Path, *, candidate_ids=None, job_id=None) -> dict[str, Any]:
        state = self._create(seed_dir, candidate_ids, job_id)
        return await self.resume(state.job_id)

    async def generate_tasks(
        self, seed_dir: Path, *, candidate_ids=None, job_id=None
    ) -> dict[str, Any]:
        state = self._create(seed_dir, candidate_ids, job_id)
        return await self.resume_generation(state.job_id)

    def _create(self, seed_dir, candidate_ids, job_id):
        self._require_access()
        source = Path(seed_dir).resolve()
        seed = SeedBundle.model_validate_json((source / "seed_bundle.json").read_text())
        seed.validate_files(source)
        if (
            self.settings.pipeline.workflow_mode == WorkflowMode.AUDITED
            and self.settings.pipeline.solution_guidance_mode == "strategy_seed"
            and seed.solution_path is None
        ):
            raise ValueError("strategy_seed requires a declared Seed solution")
        ids = candidate_ids or [
            f"candidate-{i:03d}" for i in range(1, self.settings.pipeline.task_count + 1)
        ]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be nonempty and unique")
        for name in ids:
            TypeAdapter(PathIdentifier).validate_python(name)
        job_id = job_id or f"job-{uuid.uuid4().hex[:16]}"
        root = self.job_dir(job_id)
        root.mkdir(parents=True, exist_ok=False)
        sha, config_base = freeze_config(
            self.settings.harbor.config_file,
            root / "harbor.yaml",
            audited=self.settings.pipeline.workflow_mode == WorkflowMode.AUDITED,
        )
        copy_artifact(source, root / "seed")
        state = JobState(
            job_id=job_id,
            seed_id=seed.seed_id,
            seed_dir=str(root / "seed"),
            workflow_mode=self.settings.pipeline.workflow_mode,
            config_sha256=sha,
            config_base_dir=str(self._config_base_dir or config_base),
            settings_sha256=execution_settings_digest(self.settings),
            candidates=[CandidateState(candidate_id=name) for name in ids],
        )
        JobStateStore(root).create(state)
        return state

    def _require_access(self):
        if not self.settings.allow_host_full_access:
            raise PermissionError("execution requires allow_host_full_access: true")

    async def resume_generation(self, job_id: str):
        return await self._resume(job_id, generation_only=True)

    async def resume(self, job_id: str):
        return await self._resume(job_id, generation_only=False)

    async def collect_tasks(self, job_id: str):
        return await self._resume(job_id, generation_only=False, collection_only=True)

    async def _resume(self, job_id: str, *, generation_only: bool, collection_only: bool = False):
        self._require_access()
        store = JobStateStore(self.job_dir(job_id))
        with store.lock():
            state = store.load()
            snapshot = self.job_dir(job_id) / "harbor.yaml"
            if hashlib.sha256(snapshot.read_bytes()).hexdigest() != state.config_sha256:
                raise ValueError("frozen Harbor config was modified")
            if execution_settings_digest(self.settings) != state.settings_sha256:
                raise ValueError("execution settings differ from frozen Job; start a new Job")
            state.status = JobStatus.RUNNING
            state.error = None
            await self._save(store, state, None)
            limiter = asyncio.Semaphore(self.settings.pipeline.concurrency)

            async def run_candidate(candidate):
                async with limiter:
                    if candidate.stage in {CandidateStage.CURATED, CandidateStage.FAILED}:
                        return
                    try:
                        await self._advance(
                            store, state, candidate, generation_only, collection_only
                        )
                        if candidate.stage in {
                            CandidateStage.PREFLIGHT_ACCEPTED,
                            CandidateStage.CURATED,
                        }:
                            candidate.last_error = None
                            candidate.failure_kind = None
                            await self._save(store, state, None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 保存任务失败，取消异常单独传播
                        candidate.last_error = f"{type(exc).__name__}: {exc}"
                        candidate.failure_kind = (
                            FailureKind.CONTRACT
                            if isinstance(exc, (ValueError, DeliveryContractError))
                            else FailureKind.INFRASTRUCTURE
                        )
                        # Keep the last committed stage; explicit resume can retry infrastructure.
                        await self._save(store, state, None)

            workers = [asyncio.create_task(run_candidate(c)) for c in state.candidates]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if all(c.stage == CandidateStage.CURATED for c in state.candidates):
                    state.status = JobStatus.CURATED
                elif generation_only and all(
                    c.stage in {CandidateStage.PREFLIGHT_ACCEPTED, CandidateStage.CURATED}
                    for c in state.candidates
                ):
                    state.status = JobStatus.TASKS_READY
                else:
                    state.status = JobStatus.FAILED
                    state.error = next(
                        (c.last_error for c in state.candidates if c.last_error), "interrupted"
                    )
                await self._save(store, state, None)
            return self._summary(state)

    async def _advance(self, store, state, candidate, generation_only, collection_only=False):
        while candidate.stage not in {CandidateStage.CURATED, CandidateStage.FAILED}:
            stage = candidate.stage
            if collection_only and stage not in {
                CandidateStage.PREFLIGHT_ACCEPTED,
                CandidateStage.SOLVED,
            }:
                candidate.last_error = "task requires generation before collection"
                break
            if stage == CandidateStage.PENDING:
                try:
                    await self._build(store, state, candidate)
                except DeliveryContractError as exc:
                    if exc.thread_id:
                        candidate.builder_thread_id = exc.thread_id
                    if state.workflow_mode != WorkflowMode.AUDITED:
                        raise
                    await self._diagnose_build(state, candidate, exc)
            elif stage == CandidateStage.BUILT:
                if state.workflow_mode == WorkflowMode.AUDITED and not await self._preflight(
                    store, state, candidate, task_only=generation_only
                ):
                    continue
                candidate.stage = CandidateStage.PREFLIGHT_ACCEPTED
            elif stage == CandidateStage.PREFLIGHT_ACCEPTED:
                if generation_only:
                    break
                if (
                    state.workflow_mode == WorkflowMode.AUDITED
                    and (
                        candidate.preflight_scope == "task" or candidate.strategy_card_path is None
                    )
                    and not await self._preflight(store, state, candidate, task_only=False)
                ):
                    continue
                current = [a for a in candidate.attempts if a.revision == candidate.revision]
                if sum(a.accepted for a in current) >= self.settings.pipeline.required_trajectories:
                    candidate.stage = CandidateStage.CURATED
                elif len(current) >= self.settings.pipeline.max_solver_attempts:
                    self._fail(candidate, "rollout attempt budget exhausted")
                else:
                    await self._rollout(store, state, candidate)
            elif stage == CandidateStage.SOLVED:
                attempt = candidate.attempts[-1]
                if state.workflow_mode == WorkflowMode.AUDITED:
                    if attempt.result.trajectory_path:
                        violation = atif_boundary_violation(
                            self.job_dir(state.job_id)
                            / attempt.result_dir
                            / attempt.result.trajectory_path
                        )
                        if violation:
                            self._fail(candidate, violation)
                            candidate.failure_kind = FailureKind.POLICY
                            await self._save(store, state, None)
                            continue
                    if not attempt.reviewed:
                        phase = (
                            "final"
                            if attempt.result.passed and attempt.result.trajectory_path
                            else "diagnosis"
                        )
                        review = await self._audit(state, candidate, phase, attempt)
                        attempt.reviewed = True
                        attempt.accepted = (
                            review.decision == "accept"
                            and attempt.result.passed
                            and attempt.result.trajectory_path is not None
                        )
                        if review.decision == "reject":
                            self._fail(candidate, review.summary)
                            await self._save(store, state, None)
                            continue
                        if review.decision == "revise" and review.target in {
                            "builder",
                            "distiller",
                        }:
                            self._revise(candidate, review)
                            await self._save(store, state, None)
                            continue
                else:
                    attempt.accepted = (
                        attempt.result.outcome == "completed"
                        and attempt.result.trajectory_path is not None
                    )
                candidate.stage = CandidateStage.PREFLIGHT_ACCEPTED
            await self._save(store, state, None)

    @staticmethod
    def _fail(candidate, message):
        candidate.stage = CandidateStage.FAILED
        candidate.last_error = message
        candidate.failure_kind = FailureKind.SEMANTIC

    def _revise(self, candidate, review):
        if review.decision == "revise" and review.target == "distiller":  # noqa: SIM102
            if candidate.distiller_revision < self.settings.pipeline.max_distiller_revisions:
                candidate.distiller_revision += 1
                candidate.strategy_card_path = None
                candidate.stage = CandidateStage.BUILT
                return
        if review.decision == "revise" and review.target == "builder":
            contract = (
                review.builder_reflection and review.builder_reflection.failure_class == "contract"
            )
            used = candidate.contract_repairs if contract else candidate.semantic_revisions
            maximum = (
                self.settings.pipeline.max_contract_repairs
                if contract
                else self.settings.pipeline.max_builder_revisions
            )
            if used < maximum:
                candidate.contract_repairs += int(bool(contract))
                candidate.semantic_revisions += int(not contract)
                candidate.revision += 1
                candidate.distiller_revision = 0
                candidate.strategy_card_path = None
                candidate.stage = CandidateStage.PENDING
                return
        self._fail(candidate, review.summary)

    async def _build(self, store, state, candidate):
        await self._save(store, state, f"{candidate.candidate_id}:builder:build")
        root = self.job_dir(state.job_id)
        manager = WorkspaceManager(root)
        run_id = self._run_id("builder", candidate.candidate_id, candidate.revision)
        workspace = manager.create(
            AgentRole.BUILDER, run_id, candidate_id=candidate.candidate_id, stable_builder=True
        )
        manager.stage(Path(state.seed_dir), workspace.workspace, "inputs/seed")
        feedback = None
        if candidate.task_dir:
            manager.stage(root / candidate.task_dir, workspace.workspace, "inputs/previous_task")
        previous_review = candidate.final_review or candidate.preflight_review
        if candidate.revision and previous_review:
            manager.stage(root / previous_review, workspace.workspace, "inputs/review.json")
            feedback = "inputs/review.json"
        manager.rotate_output(
            workspace.workspace / "outputs", workspace.run_dir / "previous-output"
        )
        if self.settings.pipeline.environment_strategy == "reuse_seed":
            seed_environment = Path(state.seed_dir) / "environment"
            if not seed_environment.is_dir():
                raise FileNotFoundError("reuse_seed requires a Seed environment")
            manager.stage(
                seed_environment,
                workspace.workspace,
                "outputs/harbor-task/environment",
            )

        def validate(delivery):
            if delivery.task_id != candidate.candidate_id:
                raise ValueError("Builder task_id differs from candidate")
            task = manager.resolve_output(workspace.workspace, delivery.task_dir, directory=True)
            metadata = manager.resolve_output(
                workspace.workspace, delivery.metadata_dir, directory=True
            )
            if task == metadata or task.is_relative_to(metadata) or metadata.is_relative_to(task):
                raise ValueError("task and metadata directories overlap")
            validate_harbor_delivery(
                task,
                metadata,
                expected_task_id=candidate.candidate_id,
                expected_seed_id=state.seed_id,
                environment_strategy=self.settings.pipeline.environment_strategy,
                seed_dir=Path(state.seed_dir),
            )

        image = None
        if self.settings.pipeline.environment_strategy == "reuse_seed":
            seed = SeedBundle.model_validate_json(
                (Path(state.seed_dir) / "seed_bundle.json").read_text()
            )
            if seed.environment_dir is None:
                raise ValueError("reuse_seed requires an environment")
            entry = SeedEnvironmentCache(self.settings.seed_environment_cache).resolve(
                seed.seed_id, artifact_digest(Path(state.seed_dir) / seed.environment_dir)
            )
            image = entry.image if entry else None
        execution = await self._builder().build(
            workspace,
            candidate_id=candidate.candidate_id,
            seed_id=state.seed_id,
            revision=candidate.revision,
            builder_mode=self.settings.pipeline.builder_mode,
            environment_strategy=self.settings.pipeline.environment_strategy,
            minimum_exploration_trials=self.settings.pipeline.exploration_trials,
            difficulty_policy=self.settings.pipeline.difficulty_policy,
            guidance_mode=self.settings.pipeline.solution_guidance_mode,
            workflow_mode=state.workflow_mode.value,
            thread_id=candidate.builder_thread_id,
            feedback_path=feedback,
            harbor_executable=self.settings.harbor.executable,
            prewarmed_seed_image=image,
            prewarmed_seed_docker_host=self.settings.seed_environment_cache.rollout_docker_host,
            delivery_validator=validate,
        )
        validate(execution.output)
        candidate.builder_thread_id = execution.thread_id
        revision = root / "candidates" / candidate.candidate_id / "revisions" / run_id
        copy_artifact(workspace.workspace / execution.output.task_dir, revision / "task")
        copy_artifact(workspace.workspace / execution.output.metadata_dir, revision / "metadata")
        candidate.task_dir = str((revision / "task").relative_to(root))
        candidate.metadata_dir = str((revision / "metadata").relative_to(root))
        candidate.task_sha256 = harbor_task_fingerprint(revision / "task")
        candidate.strategy_card_path = None
        candidate.stage = CandidateStage.BUILT
        candidate.last_error = None
        candidate.failure_kind = None

    async def _diagnose_build(self, state, candidate, error):
        """SDK 自修失败后由 Auditor 分类，一次修订消耗对应预算。"""
        root = self.job_dir(state.job_id)
        manager = WorkspaceManager(root)
        workspace = manager.create(
            AgentRole.AUDITOR,
            self._run_id("build-diagnosis", candidate.candidate_id, candidate.revision),
            candidate_id=candidate.candidate_id,
        )
        write_json(workspace.workspace / "inputs/failure.json", {"error": str(error)})
        execution = await self._auditor().diagnose_build(
            workspace,
            task_id=candidate.candidate_id,
            failure_context_path="inputs/failure.json",
            partial_build_root=None,
        )
        reflection = execution.output
        if not reflection.retryable:
            self._fail(candidate, reflection.summary)
            return
        review = AuditReport(
            phase="diagnosis",
            diagnosis_cause="task",
            decision="revise",
            target="builder",
            summary=reflection.summary,
            builder_reflection=reflection,
        )
        path = (
            root / "candidates" / candidate.candidate_id / "reviews" / (workspace.run_id + ".json")
        )
        write_json(path, review)
        candidate.final_review = str(path.relative_to(root))
        self._revise(candidate, review)

    async def _distill(self, state, candidate):
        root = self.job_dir(state.job_id)
        task = root / candidate.task_dir
        manager = WorkspaceManager(root)
        workspace = manager.create(
            AgentRole.DISTILLER,
            self._run_id("distiller", candidate.candidate_id, candidate.distiller_revision),
            candidate_id=candidate.candidate_id,
        )
        manager.stage(task / "instruction.md", workspace.workspace, "inputs/task/instruction.md")
        manager.stage(task / "environment", workspace.workspace, "inputs/task/environment")
        manager.stage(task / "solution", workspace.workspace, "inputs/reference")
        # Revisions get only deterministic public feedback, never raw private audit text.
        feedback = None
        if candidate.distiller_revision:
            feedback = "inputs/distiller_reflection.json"
            write_json(
                workspace.workspace / feedback,
                {"summary": "重新提炼公开任务的方法与验证策略，删除私有路径、答案和源码。"},
            )

        def validate(card):
            validate_strategy_card(
                card,
                task_id=candidate.candidate_id,
                seed_id=state.seed_id,
                public_root=workspace.workspace / "inputs/task",
                knowledge_source="target_reference",
                knowledge_root=task / "solution",
            )

        execution = await DistillerAgent(self._agent_runtime()).distill(
            workspace,
            task_id=candidate.candidate_id,
            seed_id=state.seed_id,
            revision=candidate.distiller_revision,
            knowledge_source="target_reference",
            knowledge_root="inputs/reference",
            thread_id=None,
            feedback_path=feedback,
            delivery_validator=validate,
        )
        validate(execution.output)
        path = (
            root
            / "candidates"
            / candidate.candidate_id
            / "guidance"
            / workspace.run_id
            / "strategy.json"
        )
        write_json(path, execution.output)
        candidate.strategy_card_path = str(path.relative_to(root))
        candidate.distiller_thread_id = execution.thread_id

    async def _preflight(self, store, state, candidate, *, task_only):
        if not task_only and candidate.strategy_card_path is None:
            await self._distill(state, candidate)
            await self._save(store, state, None)
        review = await self._audit(state, candidate, "preflight", task_only=task_only)
        if review.decision != "accept":
            self._revise(candidate, review)
        await self._save(store, state, None)
        return review.decision == "accept"

    async def _audit(self, state, candidate, phase, attempt=None, *, task_only=False):
        root = self.job_dir(state.job_id)
        manager = WorkspaceManager(root)
        workspace = manager.create(
            AgentRole.AUDITOR,
            self._run_id("auditor", candidate.candidate_id, candidate.revision),
            candidate_id=candidate.candidate_id,
        )
        manager.stage(root / candidate.task_dir, workspace.workspace, "inputs/task")
        manager.stage(root / candidate.metadata_dir, workspace.workspace, "inputs/metadata")
        if phase == "preflight":
            manager.stage(Path(state.seed_dir), workspace.workspace, "inputs/seed")
            if not task_only:
                manager.stage(
                    root / candidate.strategy_card_path,
                    workspace.workspace,
                    "inputs/strategy_card.json",
                )
        if attempt:
            attempt_root = root / attempt.result_dir
            manager.stage(attempt_root, workspace.workspace, "inputs/attempt")
            evidence = {
                "trajectory": attempt.result.trajectory_path,
                "trial_result": attempt.result.trial_result_path,
                "stdout": "stdout.log",
                "stderr": "stderr.log",
            }
            write_json(
                workspace.workspace / "inputs/review_context.json",
                {
                    "phase": phase,
                    "available": {
                        name: str(Path("inputs/attempt") / path)
                        for name, path in evidence.items()
                        if path and (attempt_root / path).is_file()
                    },
                    "missing": [
                        name
                        for name, path in evidence.items()
                        if not path or not (attempt_root / path).is_file()
                    ],
                    "outcome": attempt.result.outcome,
                    "error": attempt.result.error,
                },
            )

        def validate(review):
            if review.phase != phase:
                raise ValueError("audit phase mismatch")
            if task_only and review.target not in {"builder", "none"}:
                raise ValueError("task-only preflight can only request Builder revision")
            if (
                review.decision == "accept"
                and phase == "final"
                and not (attempt.result.passed and attempt.result.trajectory_path)
            ):
                raise ValueError("Auditor cannot accept a non-passing Harbor result")

        execution = await self._auditor().audit(
            workspace,
            phase=phase,
            task_id=candidate.candidate_id,
            attempt_id=attempt.attempt_id if attempt else None,
            task_only=task_only,
            delivery_validator=validate,
        )
        validate(execution.output)
        path = (
            root / "candidates" / candidate.candidate_id / "reviews" / (workspace.run_id + ".json")
        )
        write_json(path, execution.output)
        if phase == "preflight":
            candidate.preflight_review = str(path.relative_to(root))
            candidate.preflight_scope = "task" if task_only else "task_and_strategy"
        else:
            candidate.final_review = str(path.relative_to(root))
            if attempt:
                attempt.review_path = candidate.final_review
        return execution.output

    def _guidance(self, state, candidate, path):
        path.mkdir(parents=True, exist_ok=False)
        level = "unguided"
        sources = []
        if state.workflow_mode == WorkflowMode.AUDITED:
            card = SolverStrategyCard.model_validate_json(
                (self.job_dir(state.job_id) / candidate.strategy_card_path).read_text()
            )
            visible = SolverGuidanceCard.from_strategy_card(card)
            write_json(path / "strategy_card.json", visible)
            text = "授权方法指导：\n" + visible.model_dump_json(indent=2)
            level = "reference_distilled"
            sources.append(
                {
                    "path": "strategy_card.json",
                    "sha256": artifact_digest(path / "strategy_card.json"),
                }
            )
            if self.settings.pipeline.solution_guidance_mode == "strategy_seed":
                seed_root = Path(state.seed_dir)
                seed = SeedBundle.model_validate_json((seed_root / "seed_bundle.json").read_text())
                for name, relative in [
                    ("task_card", seed.task_card),
                    ("solution", seed.solution_path),
                    ("input", seed.input_dir),
                    ("software", seed.software_dir),
                ]:
                    if relative:
                        copy_artifact(seed_root / relative, path / "seed" / name)
                text += "\n授权旧任务材料位于 /opt/saf-guidance；它们不属于当前任务答案。"
                sources.append({"path": "seed", "sha256": artifact_digest(path / "seed")})
                level = "seed_guided"
            (path / "instruction.md").write_text(text)
        write_json(path / "manifest.json", {"effective_level": level, "sources": sources})
        return path

    async def _rollout(self, store, state, candidate):
        root = self.job_dir(state.job_id)
        if harbor_task_fingerprint(root / candidate.task_dir) != candidate.task_sha256:
            raise ValueError("frozen task changed")
        ordinal = len(candidate.attempts) + 1
        attempt_id = f"{candidate.candidate_id}-attempt-{ordinal:03d}"
        destination = root / "candidates" / candidate.candidate_id / "attempts" / attempt_id
        # Checkpoint includes failures as well as valid ATIF; never rerun a completed attempt.
        if (destination / "checkpoint.json").is_file():
            attempt = AttemptState.model_validate_json(
                (destination / "checkpoint.json").read_text()
            )
            if attempt.attempt_id != attempt_id or attempt.revision != candidate.revision:
                raise ValueError("attempt checkpoint identity mismatch")
        else:
            staging = destination.with_name(attempt_id + ".staging")
            if (staging / "result.json").is_file() and not (staging / "checkpoint.json").exists():
                recovered = RolloutResult.model_validate_json((staging / "result.json").read_text())
                write_json(
                    staging / "checkpoint.json",
                    AttemptState(
                        attempt_id=attempt_id,
                        revision=candidate.revision,
                        result_dir=str(destination.relative_to(root)),
                        result=recovered,
                    ),
                )
            if (staging / "checkpoint.json").is_file():
                attempt = AttemptState.model_validate_json(
                    (staging / "checkpoint.json").read_text()
                )
                if attempt.attempt_id != attempt_id or attempt.revision != candidate.revision:
                    raise ValueError("staged attempt identity mismatch")
                os.replace(staging, destination)
            else:
                if staging.exists():
                    os.replace(
                        staging,
                        staging.with_name(staging.name + ".interrupted-" + uuid.uuid4().hex[:8]),
                    )
                context = (
                    root / "candidates" / candidate.candidate_id / "contexts" / uuid.uuid4().hex
                )
                guidance = self._guidance(state, candidate, context)
                runner = self._runner or HarborRunner(
                    self.settings,
                    config_file=root / "harbor.yaml",
                    config_base_dir=Path(state.config_base_dir),
                )
                metadata = json.loads((root / candidate.metadata_dir / "metadata.json").read_text())
                result = await runner.run(
                    task_dir=root / candidate.task_dir,
                    attempt_dir=staging,
                    run_id=attempt_id,
                    guidance=guidance,
                    seed_id=state.seed_id,
                    seed_environment_sha256=metadata.get("seed_environment_sha256"),
                )
                if not isinstance(result, RolloutResult):
                    raise TypeError("runner must return RolloutResult")
                attempt = AttemptState(
                    attempt_id=attempt_id,
                    revision=candidate.revision,
                    result_dir=str(destination.relative_to(root)),
                    result=result,
                )
                write_json(staging / "checkpoint.json", attempt)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, destination)
        if attempt.result.trajectory_path and not valid_atif(
            destination / attempt.result.trajectory_path
        ):
            raise ValueError("archived attempt has an invalid ATIF")
        candidate.attempts.append(attempt)
        candidate.stage = CandidateStage.SOLVED
        await self._save(store, state, None)

    async def _save(self, store, state, active_step):
        async with self._save_lock:
            state.active_step = active_step
            store.save(state)

    def _agent_runtime(self):
        if self._runtime is None:
            self._runtime = CodexRuntime(self.settings)
        return self._runtime

    def _builder(self):
        return TaskBuilderAgent(self._agent_runtime())

    def _auditor(self):
        return AuditorAgent(self._agent_runtime())

    def status(self, job_id):
        return self._summary(JobStateStore(self.job_dir(job_id)).load())

    @staticmethod
    def _summary(state):
        return state.model_dump(mode="json")

    @staticmethod
    def _run_id(role, candidate_id, sequence):
        return f"{role}-{candidate_id}-{sequence:02d}-{uuid.uuid4().hex[:8]}"
