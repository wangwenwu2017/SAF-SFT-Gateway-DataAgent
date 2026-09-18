"""统一 Harbor 工作流的离线集成测试。

测试目的：验证真实状态机和角色隔离、审核、导出、恢复及固定批次。
被测接口：ScienceAgentFactory、BatchController、export_tool_sft。
输入与夹具：完整临时任务，文件交付型假 SDK 和结构化假 Harbor。
预期结果：设计与构建合为一次 Builder 调用，设计卡随元数据审查；三角色会话隔离，失败保留，恢复不重复执行，批次不换 Seed。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from science_agent_factory.agents.seed_environment import tree_digest
from science_agent_factory.config import FactorySettings
from science_agent_factory.contracts.deliveries import HarborBuilderDelivery, SolverStrategyCard
from science_agent_factory.contracts.reviews import AuditReport, SolverReflection
from science_agent_factory.contracts.state import RolloutResult
from science_agent_factory.export.harbor import export_harbor_job
from science_agent_factory.export.sft import export_tool_sft
from science_agent_factory.storage.files import copy_artifact, remove_path
from science_agent_factory.storage.json import write_json
from science_agent_factory.workflow.batch import BatchController
from science_agent_factory.workflow.factory import ScienceAgentFactory
from tests.unit.test_harbor_task import (
    _difficulty_assessment,
    _harbor_metadata,
    _harbor_task,
    _seed_bundle,
)

MAPPING = {
    "entries": [
        {
            "mapping_id": "core",
            "seed_capability": "compute",
            "candidate_capability": "compute",
            "relation": "preserved",
            "core": True,
            "evidence": "same skill",
        }
    ],
    "summary": "preserved",
}


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.accept_failure = False

    async def run_to_file(
        self, role, workspace, message, output_type, output_relative_path, **kwargs
    ):
        request = json.loads(message)
        self.calls.append((role, request, workspace.workspace))
        task_id = request.get("task_id", request.get("candidate_id"))
        root = workspace.workspace
        if output_type is HarborBuilderDelivery:
            seed_id = json.loads((root / "inputs/seed/seed_bundle.json").read_text())["seed_id"]
            _harbor_task(root / "outputs", task_id)
            _harbor_metadata(root / "outputs", task_id=task_id, seed_id=seed_id)
            output = HarborBuilderDelivery(task_id=task_id, summary="built")
        elif output_type is SolverStrategyCard:
            assert not (root / "inputs/task/tests").exists()
            output = SolverStrategyCard(
                task_id=task_id,
                seed_id=request["seed_id"],
                objective="compute",
                task_difference="new case",
                method_steps=["derive formula"],
                tool_patterns=["python"],
                decision_points=["check precision"],
                invariants=["finite"],
                failure_modes=["rounding"],
                verification_strategy=["check residual"],
                public_contract_refs=["instruction.md"],
                excluded_instance_details=["values"],
            )
        elif output_type is AuditReport:
            failed = False
            if request["phase"] in {"final", "diagnosis"}:
                result = RolloutResult.model_validate_json(
                    (root / "inputs/attempt/result.json").read_text()
                )
                failed = (
                    not result.passed or not result.trajectory_path
                ) and not self.accept_failure
            output = AuditReport(
                phase=request["phase"],
                decision="revise" if failed else "accept",
                target="solver" if failed else "none",
                diagnosis_cause="unknown" if request["phase"] == "diagnosis" else None,
                solver_reflection=SolverReflection(
                    source_attempt_id=request["attempt_id"],
                    failure_class="execution",
                    symptom_codes=["incomplete_delivery"],
                    public_contract_refs=["instruction.md"],
                    summary="independent attempt needed",
                    avoidance_advice=["check public requirements"],
                )
                if failed
                else None,
                summary="evidence reviewed",
                difficulty_assessment=_difficulty_assessment(),
            )
        else:
            raise AssertionError(output_type)
        write_json(root / output_relative_path, output)
        workspace.event_path.parent.mkdir(parents=True, exist_ok=True)
        workspace.event_path.write_text("")
        if kwargs.get("delivery_validator"):
            kwargs["delivery_validator"](output)
        return SimpleNamespace(output=output, thread_id="thread", event_path=workspace.event_path)


class SeedCopyRuntime(FakeRuntime):
    """Builder 替身：确认 reuse_seed 环境已由工厂预置，并据此交付 seed_copy 元数据。"""

    async def run_to_file(
        self, role, workspace, message, output_type, output_relative_path, **kwargs
    ):
        if output_type is not HarborBuilderDelivery:
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )
        request = json.loads(message)
        root = workspace.workspace
        seed_id = json.loads((root / "inputs/seed/seed_bundle.json").read_text())["seed_id"]
        seed_environment = root / "inputs/seed/environment"
        task_environment = root / "outputs/harbor-task/environment"
        assert (task_environment / "Dockerfile").read_text() == (
            seed_environment / "Dockerfile"
        ).read_text()
        remove_path(task_environment)
        _harbor_task(root / "outputs", request["candidate_id"])
        # _harbor_task 重建了 environment，恢复为工厂预置的 Seed 副本以满足 reuse_seed 契约。
        copy_artifact(seed_environment, task_environment, replace=True)
        _harbor_metadata(
            root / "outputs",
            task_id=request["candidate_id"],
            seed_id=seed_id,
            environment_source="seed_copy",
            seed_digest=tree_digest(seed_environment),
        )
        output = HarborBuilderDelivery(task_id=request["candidate_id"], summary="built")
        write_json(root / output_relative_path, output)
        workspace.event_path.parent.mkdir(parents=True, exist_ok=True)
        workspace.event_path.write_text("")
        if kwargs.get("delivery_validator"):
            kwargs["delivery_validator"](output)
        return SimpleNamespace(output=output, thread_id="thread", event_path=workspace.event_path)


class FakeRunner:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or [("completed", 1)]
        self.calls = []
        self.cancel_after_result = False

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        root = kwargs["attempt_dir"]
        root.mkdir(parents=True)
        copy_artifact(kwargs["guidance"], root / "guidance")
        outcome, reward = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        write_json(
            root / "trajectory.json",
            {
                "schema_version": "ATIF-v1.6",
                "steps": [
                    {"source": "user", "message": "actual task"},
                    {
                        "source": "agent",
                        "reasoning_content": "actual reasoning",
                        "message": "done",
                        "tool_calls": [
                            {
                                "tool_call_id": "1",
                                "function_name": "shell",
                                "arguments": {"command": "compute"},
                            }
                        ],
                        "observation": {"results": [{"source_call_id": "1", "content": "42"}]},
                    },
                ],
            },
        )
        result = RolloutResult(
            outcome=outcome,
            trajectory_path="trajectory.json",
            agent="custom-agent",
            model="custom-model",
            verification_executed=reward is not None,
            rewards={"reward": reward} if reward is not None else None,
        )
        write_json(root / "result.json", result)
        if self.cancel_after_result:
            raise asyncio.CancelledError
        return result


def settings_for(tmp_path, mode="lightweight"):
    settings = FactorySettings.load_default()
    settings.storage_root = tmp_path / "runs"
    settings.allow_host_full_access = True
    settings.pipeline.workflow_mode = type(settings.pipeline.workflow_mode)(mode)
    native = tmp_path / "harbor.yaml"
    native.write_text("agents: [{name: arbitrary-agent, model_name: arbitrary-model}]\n")
    settings.harbor.config_file = native
    return settings


@pytest.mark.parametrize("mode", ["lightweight", "audited"])
async def test_end_to_end(tmp_path, mode):
    settings = settings_for(tmp_path, mode)
    seed, _ = _seed_bundle(tmp_path)
    runtime, runner = FakeRuntime(), FakeRunner()
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=runner)
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "curated", result
    assert len(runner.calls) == 1
    builder_calls = [request for role, request, _ in runtime.calls if role == "builder"]
    assert len(builder_calls) == 1
    assert builder_calls[0]["phase"] == "build"
    assert builder_calls[0]["seed_id"] == "seed-001"
    assert all(request.get("phase") != "design" for _, request, _ in runtime.calls)
    for role, request, workspace in runtime.calls:
        if role == "auditor" and request["phase"] == "preflight":
            assert (workspace / "inputs/metadata/design_card.json").is_file()
    roles = {role for role, _, _ in runtime.calls}
    assert roles == ({"builder"} if mode == "lightweight" else {"builder", "distiller", "auditor"})
    job = factory.job_dir("job")
    record = json.loads(export_tool_sft(job).read_text())
    assert record["agent"] == "custom-agent"
    assert record["context"]["actual_user_message"] == "actual task"
    assert record["quality"]["training_eligible"] == (mode == "audited")
    assert any(e["kind"] == "reasoning" for e in record["events"])
    exported = export_harbor_job(job)
    assert (exported / "tasks/candidate-001/solution/solve.sh").exists()
    await factory.resume("job")
    assert len(runner.calls) == 1


async def test_reuse_seed_environment_is_materialized_by_factory(tmp_path):
    settings = settings_for(tmp_path, "lightweight")
    settings.pipeline.environment_strategy = "reuse_seed"
    seed, _ = _seed_bundle(tmp_path)
    factory = ScienceAgentFactory(settings, runtime=SeedCopyRuntime(), runner=FakeRunner())
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "curated", (
        result["candidates"][0]["last_error"],
        result["candidates"][0]["failure_kind"],
    )
    revisions = factory.job_dir("job") / "candidates/candidate-001/revisions"
    copied = list(revisions.glob("*/task/environment/Dockerfile"))
    assert copied and copied[0].read_text() == (seed / "environment/Dockerfile").read_text()


async def test_failure_export_and_audited_selection(tmp_path):
    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    runner = FakeRunner([("timeout", 1), ("completed", 1)])
    factory = ScienceAgentFactory(settings, runtime=FakeRuntime(), runner=runner)
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "curated", result
    job = factory.job_dir("job")
    selected = [json.loads(x) for x in export_tool_sft(job).read_text().splitlines()]
    all_records = [
        json.loads(x) for x in export_tool_sft(job, selection="all").read_text().splitlines()
    ]
    assert len(selected) == 1 and len(all_records) == 2
    assert all_records[0]["outcome"] == "timeout"
    assert not all_records[0]["quality"]["training_eligible"]


async def test_unverified_lightweight_is_archived(tmp_path):
    settings = settings_for(tmp_path)
    seed, _ = _seed_bundle(tmp_path)
    factory = ScienceAgentFactory(
        settings, runtime=FakeRuntime(), runner=FakeRunner([("completed", None)])
    )
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "curated", result
    record = json.loads(export_tool_sft(factory.job_dir("job")).read_text())
    assert not record["verification"]["executed"]
    assert not record["quality"]["training_eligible"]


async def test_false_auditor_accept_is_rejected(tmp_path):
    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    runtime = FakeRuntime()
    runtime.accept_failure = True
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=FakeRunner([("completed", 0)]))
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "failed"
    assert "diagnosis cannot accept" in result["error"]
    assert export_tool_sft(factory.job_dir("job")).read_text() == ""


async def test_recover_result_before_checkpoint_without_new_run(tmp_path):
    settings = settings_for(tmp_path)
    seed, _ = _seed_bundle(tmp_path)
    runner = FakeRunner()
    runner.cancel_after_result = True
    factory = ScienceAgentFactory(settings, runtime=FakeRuntime(), runner=runner)
    with pytest.raises(asyncio.CancelledError):
        await factory.run(seed, job_id="job")
    runner.cancel_after_result = False
    result = await factory.resume("job")
    assert result["status"] == "curated", result
    assert len(runner.calls) == 1


async def test_distiller_contract_failure_preserves_builder_session(tmp_path):
    from science_agent_factory.agents.runtime import DeliveryContractError

    class Runtime(FakeRuntime):
        fail_distiller = True

        async def run_to_file(self, role, workspace, message, output_type, *args, **kwargs):
            if output_type is SolverStrategyCard and self.fail_distiller:
                raise DeliveryContractError("invalid strategy", thread_id="distiller-thread")
            return await super().run_to_file(
                role, workspace, message, output_type, *args, **kwargs
            )

    seed, _ = _seed_bundle(tmp_path)
    runtime = Runtime()
    factory = ScienceAgentFactory(
        settings_for(tmp_path, "audited"), runtime=runtime, runner=FakeRunner()
    )
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "failed"
    state = json.loads((factory.job_dir("job") / "state.json").read_text())
    assert state["candidates"][0]["builder_thread_id"] == "thread"
    runtime.fail_distiller = False
    assert (await factory.resume("job"))["status"] == "curated"
    assert sum(call[0] == "builder" for call in runtime.calls) == 1


async def test_generation_collection_and_config_freeze(tmp_path):
    settings = settings_for(tmp_path)
    seed, _ = _seed_bundle(tmp_path)
    runner = FakeRunner()
    factory = ScienceAgentFactory(settings, runtime=FakeRuntime(), runner=runner)
    result = await factory.generate_tasks(seed, job_id="job")
    assert result["status"] == "tasks_ready", result
    assert not runner.calls
    settings.harbor.config_file.write_text("agents: [{name: different}]\n")
    assert (await factory.collect_tasks("job"))["status"] == "curated"
    assert "arbitrary-agent" in (factory.job_dir("job") / "harbor.yaml").read_text()
    (factory.job_dir("job") / "harbor.yaml").write_text("agents: [{name: changed}]")
    with pytest.raises(ValueError, match="modified"):
        await factory.resume("job")


async def test_fixed_batch_retains_failed_seed_and_exports_others(tmp_path):
    settings = settings_for(tmp_path)
    settings.batch.max_generation_resumes = 0
    seed_root = tmp_path / "seeds"
    entries = []
    for name in ["one", "two"]:
        seed, _ = _seed_bundle(seed_root / name, seed_id=name)
        entries.append(
            {"seed_id": name, "path": str(seed.relative_to(seed_root)), "status": "included"}
        )
    write_json(
        seed_root / "import_manifest.json",
        {"schema_version": "saf.seed-import.v1", "entries": entries},
    )

    class Factory(ScienceAgentFactory):
        def __init__(self, settings, **kwargs):
            super().__init__(settings, runtime=FakeRuntime(), runner=FakeRunner(), **kwargs)

        async def generate_tasks(self, seed_dir, **kwargs):
            if "two" in str(seed_dir):
                raise OSError("unavailable")
            return await super().generate_tasks(seed_dir, **kwargs)

    controller = BatchController(settings, factory_type=Factory)
    result = await controller.run(seed_root, batch_id="batch")
    assert result["status"] == "blocked", result
    assert [t["seed_id"] for t in result["tasks"]] == ["one", "two"]
    assert [t["status"] for t in result["tasks"]] == ["complete", "failed"]
    report = controller.export("batch")
    assert report["failed_seeds"] == ["two"]
    assert len(report["tasks"]) == 1
    assert report["samples"] == 1


@pytest.mark.parametrize("name", ["../escape", "/absolute", ".", "..", "a/b"])
def test_job_paths_reject_traversal(tmp_path, name):
    with pytest.raises(ValueError):
        ScienceAgentFactory(settings_for(tmp_path)).job_dir(name)


async def test_strategy_seed_exposes_only_declared_material(tmp_path):
    settings = settings_for(tmp_path, "audited")
    settings.pipeline.solution_guidance_mode = "strategy_seed"
    seed, _ = _seed_bundle(tmp_path)
    (seed / "secret-extra.txt").write_text("not authorized")
    factory = ScienceAgentFactory(settings, runtime=FakeRuntime(), runner=FakeRunner())
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "curated", result
    record = json.loads(export_tool_sft(factory.job_dir("job")).read_text())
    assert record["context"]["guidance_level"] == "seed_guided"
    guidance = (
        factory.job_dir("job") / result["candidates"][0]["attempts"][0]["result_dir"] / "guidance"
    )
    assert sorted(p.name for p in (guidance / "seed").iterdir()) == ["solution", "task_card"]
    assert "seed_id" not in (guidance / "strategy_card.json").read_text()


async def test_boundary_checks_tool_input_not_observation(tmp_path):
    from science_agent_factory.workflow.guidance import atif_boundary_violation

    trajectory = tmp_path / "trajectory.json"
    payload = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [{"arguments": {"command": "cat /workspace/output"}}],
                "observation": {"results": [{"content": "/solution/solve.sh"}]},
            }
        ]
    }
    write_json(trajectory, payload)
    assert atif_boundary_violation(trajectory) is None
    payload["steps"][0]["tool_calls"][0]["arguments"]["command"] = "cat /solution/solve.sh"
    write_json(trajectory, payload)
    assert atif_boundary_violation(trajectory)
    payload["steps"][0]["tool_calls"][0]["arguments"]["command"] = "cat /opt/saf-guidance/solution"
    write_json(trajectory, payload)
    assert atif_boundary_violation(trajectory) is None


async def test_audited_builder_contract_repair_is_bounded(tmp_path):
    from science_agent_factory.agents.runtime import DeliveryContractError
    from science_agent_factory.contracts.reviews import BuilderReflection

    class BrokenBuild(FakeRuntime):
        async def run_to_file(
            self, role, workspace, message, output_type, output_relative_path, **kwargs
        ):
            if output_type is HarborBuilderDelivery:
                raise DeliveryContractError("invalid artifact", thread_id="builder-original")
            if output_type is BuilderReflection:
                output = BuilderReflection(
                    source_phase="build_delivery",
                    failure_class="contract",
                    issue_codes=["INVALID_ARTIFACT"],
                    summary="fix artifact",
                    evidence=["invalid artifact"],
                    required_actions=["repair artifact"],
                    avoid_actions=["do not change semantics"],
                    retryable=True,
                )
                return SimpleNamespace(output=output)
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )

    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    factory = ScienceAgentFactory(settings, runtime=BrokenBuild(), runner=FakeRunner())
    result = await factory.run(seed, job_id="job")
    candidate = result["candidates"][0]
    assert result["status"] == "failed"
    assert candidate["stage"] == "failed"
    assert candidate["contract_repairs"] == 1
    assert candidate["semantic_revisions"] == 0
    assert candidate["builder_thread_id"] == "builder-original"


@pytest.mark.parametrize("collection_enabled", [False, True])
async def test_batch_collects_ready_candidate_when_peer_generation_fails(
    tmp_path, collection_enabled
):
    settings = settings_for(tmp_path)
    settings.batch.tasks_per_seed = 2
    settings.batch.trajectory_concurrency = int(collection_enabled)
    settings.batch.max_generation_resumes = 0
    root = tmp_path / "seeds"
    _seed_bundle(root)
    write_json(
        root / "import_manifest.json",
        {
            "schema_version": "saf.seed-import.v1",
            "entries": [{"status": "included", "seed_id": "seed-001", "path": "seed"}],
        },
    )

    class PartialRuntime(FakeRuntime):
        async def run_to_file(
            self, role, workspace, message, output_type, output_relative_path, **kwargs
        ):
            if output_type is HarborBuilderDelivery and json.loads(message)["candidate_id"].endswith(
                "002"
            ):
                raise OSError("one slot unavailable")
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )

    class Factory(ScienceAgentFactory):
        def __init__(self, settings, **kwargs):
            super().__init__(settings, runtime=PartialRuntime(), runner=FakeRunner(), **kwargs)

    controller = BatchController(settings, factory_type=Factory)
    result = await controller.run(root, batch_id="batch")
    assert result["status"] == "blocked"
    report = controller.export("batch")
    assert report["samples"] == int(collection_enabled)
    assert len(report["tasks"]) == 1
    assert result["tasks"][0]["status"] == "failed"
    assert result["error"]


async def test_collection_starts_before_last_generation_finishes(tmp_path):
    settings = settings_for(tmp_path)
    settings.batch.generation_concurrency = 2
    root = tmp_path / "seeds"
    entries = []
    for name in ["one", "two"]:
        seed, _ = _seed_bundle(root / name, seed_id=name)
        entries.append({"status": "included", "seed_id": name, "path": str(seed.relative_to(root))})
    write_json(
        root / "import_manifest.json", {"schema_version": "saf.seed-import.v1", "entries": entries}
    )
    collected = asyncio.Event()

    class Factory(ScienceAgentFactory):
        def __init__(self, settings, **kwargs):
            super().__init__(settings, runtime=FakeRuntime(), runner=FakeRunner(), **kwargs)

        async def generate_tasks(self, seed_dir, **kwargs):
            if "two" in str(seed_dir):
                await asyncio.wait_for(collected.wait(), timeout=3)
            return await super().generate_tasks(seed_dir, **kwargs)

        async def resume(self, job_id):
            result = await super().resume(job_id)
            collected.set()
            return result

    result = await BatchController(settings, factory_type=Factory).run(root, batch_id="batch")
    assert result["status"] == "curated", result
    assert collected.is_set()


async def test_obsolete_task_revision_is_diagnostic_only(tmp_path):
    from science_agent_factory.workflow.state import JobStateStore

    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    factory = ScienceAgentFactory(settings, runtime=FakeRuntime(), runner=FakeRunner())
    await factory.run(seed, job_id="job")
    root = factory.job_dir("job")
    store = JobStateStore(root)
    state = store.load()
    state.candidates[0].revision += 1
    store.save(state)
    assert export_tool_sft(root).read_text() == ""
    record = json.loads(export_tool_sft(root, selection="all").read_text())
    assert not record["quality"]["training_eligible"]


async def test_successful_resume_clears_stale_candidate_error(tmp_path):
    class TransientRunner(FakeRunner):
        failed = False

        async def run(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("temporary infrastructure failure")
            return await super().run(**kwargs)

    settings = settings_for(tmp_path)
    seed, _ = _seed_bundle(tmp_path)
    factory = ScienceAgentFactory(settings, runtime=FakeRuntime(), runner=TransientRunner())
    result = await factory.run(seed, job_id="job")
    assert result["candidates"][0]["last_error"]
    recovered = await factory.resume("job")
    assert recovered["status"] == "curated"
    assert recovered["candidates"][0]["last_error"] is None
    assert recovered["candidates"][0]["failure_kind"] is None


class MissingTrajectoryRunner(FakeRunner):
    """模拟 Harbor 在错误或取消时只保存了部分诊断证据。"""

    async def run(self, **kwargs):
        result = await super().run(**kwargs)
        root = kwargs["attempt_dir"]
        (root / "trajectory.json").unlink()
        (root / "stderr.log").write_text("environment startup failed")
        result.trajectory_path = None
        result.error = "no valid ATIF"
        write_json(root / "result.json", result)
        return result


@pytest.mark.parametrize("outcome", ["error", "timeout", "cancelled"])
async def test_no_trajectory_still_receives_diagnosis_and_respects_budget(tmp_path, outcome):
    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    runner = MissingTrajectoryRunner([(outcome, None)])
    runtime = FakeRuntime()
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=runner)
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "failed"
    assert len(runner.calls) == settings.pipeline.max_solver_attempts
    attempts = result["candidates"][0]["attempts"]
    assert all(a["reviewed"] and a["review_path"] and not a["accepted"] for a in attempts)
    diagnoses = [
        (request, root) for _, request, root in runtime.calls if request.get("phase") == "diagnosis"
    ]
    assert len(diagnoses) == len(attempts)
    for request, root in diagnoses:
        context = json.loads((root / request["review_context_path"]).read_text())
        assert "trajectory" in context["missing"]
        assert "stderr" in context["available"]
    assert export_tool_sft(factory.job_dir("job"), selection="all").read_text() == ""


@pytest.mark.parametrize("missing_trajectory", [False, True])
async def test_auditor_failure_resumes_review_without_rerunning_solver(
    tmp_path, missing_trajectory
):
    class FailingAuditor(FakeRuntime):
        failed = False

        async def run_to_file(
            self, role, workspace, message, output_type, output_relative_path, **kwargs
        ):
            if json.loads(message).get("phase") in {"final", "diagnosis"} and not self.failed:
                self.failed = True
                raise OSError("auditor unavailable")
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )

    settings = settings_for(tmp_path, "audited")
    settings.pipeline.max_solver_attempts = 1
    seed, _ = _seed_bundle(tmp_path)
    runtime = FailingAuditor()
    runner = MissingTrajectoryRunner([("error", None)]) if missing_trajectory else FakeRunner()
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=runner)
    interrupted = await factory.run(seed, job_id="job")
    assert interrupted["candidates"][0]["stage"] == "solved"
    assert not interrupted["candidates"][0]["attempts"][0]["reviewed"]
    recovered = await factory.resume("job")
    assert len(runner.calls) == 1
    assert recovered["candidates"][0]["attempts"][0]["reviewed"]
    assert recovered["status"] == ("failed" if missing_trajectory else "curated")


@pytest.mark.parametrize("outcome,reward", [("completed", 1), ("error", None)])
async def test_auditor_rejection_stops_candidate(tmp_path, outcome, reward):
    class RejectingAuditor(FakeRuntime):
        async def run_to_file(
            self, role, workspace, message, output_type, output_relative_path, **kwargs
        ):
            request = json.loads(message)
            if request.get("phase") in {"final", "diagnosis"}:
                output = AuditReport(
                    phase=request["phase"],
                    decision="reject",
                    target="none",
                    diagnosis_cause="unknown" if request["phase"] == "diagnosis" else None,
                    summary="insufficient evidence to continue",
                )
                kwargs["delivery_validator"](output)
                return SimpleNamespace(output=output)
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )

    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    runner = FakeRunner([(outcome, reward)])
    factory = ScienceAgentFactory(settings, runtime=RejectingAuditor(), runner=runner)
    result = await factory.run(seed, job_id="job")
    assert result["candidates"][0]["stage"] == "failed"
    assert len(runner.calls) == 1
    assert result["candidates"][0]["attempts"][0]["reviewed"]


async def test_diagnosed_task_defect_triggers_builder_revision(tmp_path):
    from science_agent_factory.contracts.reviews import BuilderReflection

    class RepairAuditor(FakeRuntime):
        async def run_to_file(
            self, role, workspace, message, output_type, output_relative_path, **kwargs
        ):
            if json.loads(message).get("phase") == "diagnosis":
                output = AuditReport(
                    phase="diagnosis",
                    diagnosis_cause="task",
                    decision="revise",
                    target="builder",
                    summary="fix task environment",
                    builder_reflection=BuilderReflection(
                        source_phase="diagnosis",
                        failure_class="environment",
                        issue_codes=["ENVIRONMENT_FAILED"],
                        summary="repair environment",
                        evidence=["startup failure"],
                        required_actions=["fix dependency"],
                        avoid_actions=["do not relax tests"],
                        retryable=True,
                    ),
                )
                kwargs["delivery_validator"](output)
                return SimpleNamespace(output=output)
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )

    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    runner = FakeRunner([("error", None), ("completed", 1)])
    runtime = RepairAuditor()
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=runner)
    result = await factory.run(seed, job_id="job")
    assert result["status"] == "curated", result
    assert result["candidates"][0]["revision"] == 1
    assert len(runner.calls) == 2
    assert [a["revision"] for a in result["candidates"][0]["attempts"]] == [0, 1]
    builds = [request for role, request, _ in runtime.calls if role == "builder"]
    assert [request["revision"] for request in builds] == [0, 1]
    assert all(request.get("phase") != "design" for _, request, _ in runtime.calls)
    cards = list((factory.job_dir("job") / "candidates/candidate-001/revisions").glob(
        "*/metadata/design_card.json"
    ))
    assert len(cards) == 2


async def test_cancelled_attempt_is_diagnosed_only_after_explicit_resume(tmp_path):
    class CancellingRunner(MissingTrajectoryRunner):
        async def run(self, **kwargs):
            await super().run(**kwargs)
            raise asyncio.CancelledError

    settings = settings_for(tmp_path, "audited")
    settings.pipeline.max_solver_attempts = 1
    seed, _ = _seed_bundle(tmp_path)
    runtime = FakeRuntime()
    runner = CancellingRunner([("cancelled", None)])
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=runner)
    with pytest.raises(asyncio.CancelledError):
        await factory.run(seed, job_id="job")
    assert not any(request.get("phase") == "diagnosis" for _, request, _ in runtime.calls)
    result = await factory.resume("job")
    assert len(runner.calls) == 1
    assert result["candidates"][0]["attempts"][0]["reviewed"]
    assert any(request.get("phase") == "diagnosis" for _, request, _ in runtime.calls)


async def test_real_runner_launch_failure_reaches_auditor(tmp_path):
    settings = settings_for(tmp_path, "audited")
    settings.pipeline.max_solver_attempts = 1
    settings.harbor.executable = str(tmp_path / "not-installed")
    seed, _ = _seed_bundle(tmp_path)
    runtime = FakeRuntime()
    factory = ScienceAgentFactory(settings, runtime=runtime)
    result = await factory.run(seed, job_id="job")
    attempt = result["candidates"][0]["attempts"][0]
    assert attempt["result"]["outcome"] == "error"
    assert "Harbor executable not found" in attempt["result"]["error"]
    assert attempt["result"]["trajectory_path"] is None
    assert attempt["reviewed"] and attempt["review_path"]
    assert any(request.get("phase") == "diagnosis" for _, request, _ in runtime.calls)


async def test_interrupted_build_resumes_without_design_stage(tmp_path):
    class InterruptedBuild(FakeRuntime):
        interrupted = False

        async def run_to_file(self, role, workspace, message, output_type,
                              output_relative_path, **kwargs):
            if output_type is HarborBuilderDelivery and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError
            return await super().run_to_file(
                role, workspace, message, output_type, output_relative_path, **kwargs
            )

    settings = settings_for(tmp_path, "audited")
    seed, _ = _seed_bundle(tmp_path)
    runtime, runner = InterruptedBuild(), FakeRunner()
    factory = ScienceAgentFactory(settings, runtime=runtime, runner=runner)
    with pytest.raises(asyncio.CancelledError):
        await factory.run(seed, job_id="job")
    state = json.loads((factory.job_dir("job") / "state.json").read_text())
    assert state["candidates"][0]["stage"] == "pending"
    assert not runner.calls
    result = await factory.resume("job")
    assert result["status"] == "curated"
    assert len(runner.calls) == 1
    assert all(request.get("phase") != "design" for _, request, _ in runtime.calls)
