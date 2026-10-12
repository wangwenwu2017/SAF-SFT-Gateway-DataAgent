"""内部 SDK 三角色的任务消息和交付边界。

模块用途：内部 SDK 三角色的任务消息和交付边界；Auditor 区分仅任务及任务加策略预审；Builder 一次调用完成设计与构建。
主要接口：AgentRuntime、TaskBuilderAgent、DistillerAgent、AuditorAgent。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：调用注入的 SDK 运行时；不执行 Solver、不管理工作流状态。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

from ..contracts.deliveries import HarborBuilderDelivery, SolverStrategyCard
from ..contracts.enums import AgentRole
from ..contracts.reviews import AuditReport, BuilderReflection
from .runtime import AgentExecution

if TYPE_CHECKING:
    from ..workflow.workspace import AgentWorkspace

OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentRuntime(Protocol):
    async def run_to_file(
        self,
        role: AgentRole,
        agent_workspace: AgentWorkspace,
        message: str,
        output_type: type[OutputT],
        output_relative_path: str,
        *,
        thread_id: str | None = None,
        persistent: bool = False,
        prompt_phase: str | None = None,
        delivery_validator: Callable[[OutputT], None] | None = None,
    ) -> AgentExecution[OutputT]: ...


class TaskBuilderAgent:
    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime

    async def build(
        self,
        workspace: AgentWorkspace,
        *,
        candidate_id: str,
        seed_id: str,
        revision: int,
        builder_mode: str,
        environment_strategy: str,
        minimum_exploration_trials: int,
        difficulty_policy: str,
        guidance_mode: str,
        workflow_mode: str,
        thread_id: str | None,
        feedback_path: str | None,
        harbor_executable: str = "harbor",
        prewarmed_seed_image: str | None = None,
        prewarmed_seed_docker_host: str | None = None,
        delivery_validator: Callable[[HarborBuilderDelivery], None] | None = None,
    ) -> AgentExecution[HarborBuilderDelivery]:
        request = {
            "role": "task_builder",
            "phase": "build",
            "candidate_id": candidate_id,
            "seed_id": seed_id,
            "revision": revision,
            "builder_mode": builder_mode,
            "environment_strategy": environment_strategy,
            "exploration_policy": {
                "mode": ("autonomous" if minimum_exploration_trials == 0 else "minimum_required"),
                "minimum_additional_trials": minimum_exploration_trials,
            },
            "difficulty_policy": difficulty_policy,
            "solution_guidance_mode": guidance_mode,
            "workflow_mode": workflow_mode,
            "harbor_executable": harbor_executable,
            "prewarmed_seed_image": (prewarmed_seed_image),
            "prewarmed_seed_docker_host": (prewarmed_seed_docker_host),
            "seed_root": "inputs/seed",
            "previous_task": "inputs/previous_task" if revision else None,
            "review_feedback": feedback_path,
            "goal": "在本次构建中完成设计卡、完整 Harbor Task 和绑定任务摘要的真实 Oracle=1、负例=0 自测。",
        }
        return await self.runtime.run_to_file(
            AgentRole.BUILDER,
            workspace,
            json.dumps(request, ensure_ascii=False, indent=2),
            HarborBuilderDelivery,
            "outputs/build_result.json",
            thread_id=thread_id,
            persistent=True,
            prompt_phase="harbor_build",
            delivery_validator=delivery_validator,
        )


class DistillerAgent:
    """把已验证的当前任务 Reference 压缩为不含完整答案的策略卡。"""

    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime

    async def distill(
        self,
        workspace: AgentWorkspace,
        *,
        task_id: str,
        seed_id: str,
        revision: int,
        knowledge_source: str,
        knowledge_root: str,
        thread_id: str | None,
        feedback_path: str | None,
        delivery_validator: Callable[[SolverStrategyCard], None] | None = None,
    ) -> AgentExecution[SolverStrategyCard]:
        request = {
            "role": "distiller",
            "task_id": task_id,
            "seed_id": seed_id,
            "revision": revision,
            "public_task_root": "inputs/task",
            "knowledge_source": knowledge_source,
            "knowledge_root": knowledge_root,
            "previous_strategy_card": (None),
            "review_feedback": feedback_path,
            "goal": (
                "依据当前公开契约和指定的私有知识来源，提炼足以独立重建解法的算法蓝图、"
                "参数推导、决策点和验证策略；不得输出完整代码、目标输出或私有验证信息。"
            ),
        }
        return await self.runtime.run_to_file(
            AgentRole.DISTILLER,
            workspace,
            json.dumps(request, ensure_ascii=False, indent=2),
            SolverStrategyCard,
            "outputs/strategy_card.json",
            thread_id=thread_id,
            persistent=True,
            delivery_validator=delivery_validator,
        )


class AuditorAgent:
    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime

    async def audit(
        self,
        workspace: AgentWorkspace,
        *,
        phase: str,
        task_id: str,
        attempt_id: str | None = None,
        task_only: bool = False,
        delivery_validator=None,
    ) -> AgentExecution[AuditReport]:
        request = {
            "phase": phase,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "task_root": "inputs/task",
            "metadata_root": "inputs/metadata",
            "review_scope": ("task" if task_only else "task_and_strategy")
            if phase == "preflight"
            else "trajectory",
            "strategy_card_path": (
                "inputs/strategy_card.json" if phase == "preflight" and not task_only else None
            ),
            "attempt_root": "inputs/attempt" if attempt_id else None,
            "review_context_path": "inputs/review_context.json" if attempt_id else None,
            "goal": "只读审查任务质量、指导边界及真实 Harbor 结果；不得执行验证或修改任务。",
        }
        return await self.runtime.run_to_file(
            AgentRole.AUDITOR,
            workspace,
            json.dumps(request, ensure_ascii=False),
            AuditReport,
            "outputs/audit.json",
            prompt_phase="preflight" if phase == "preflight" else "trajectory",
            delivery_validator=delivery_validator,
        )

    async def diagnose_build(
        self,
        workspace: AgentWorkspace,
        *,
        task_id: str,
        failure_context_path: str,
        partial_build_root: str | None,
        delivery_validator: Callable[[BuilderReflection], None] | None = None,
    ) -> AgentExecution[BuilderReflection]:
        request = {
            "role": "auditor",
            "phase": "build_diagnosis",
            "task_id": task_id,
            "failure_context_path": failure_context_path,
            "partial_build_root": partial_build_root,
            "goal": (
                "只读取失败上下文与安全暂存的部分产物，归因 Builder 构建失败并给出一次结构化反思；"
                "不得执行尚未通过结构校验的脚本。"
            ),
        }
        return await self.runtime.run_to_file(
            AgentRole.AUDITOR,
            workspace,
            json.dumps(request, ensure_ascii=False, indent=2),
            BuilderReflection,
            "outputs/builder_reflection.json",
            prompt_phase="build_diagnosis",
            delivery_validator=delivery_validator,
        )
