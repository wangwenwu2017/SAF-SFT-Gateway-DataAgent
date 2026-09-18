"""工厂、Agent、Harbor 与批次配置加载。

模块用途：配置仅路由构建和后续审查阶段；集中定义工厂、Agent、Harbor 与批次配置加载，不承担工作流编排。
主要接口：AgentProfile、CodexSettings、SeedEnvironmentCacheSettings、HarborSettings、PipelineSettings、BatchSettings、FactorySettings。
输入：配置字段（包括 Builder 探索软预算秒数，0 禁用提醒）、制品数据或状态字典。
输出：校验后的模型、规范化值或异常。
副作用与边界：配置加载读取 YAML、环境变量与包内资源；不启动 Agent。
"""

from __future__ import annotations

import os
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from .base import StrictModel
from .enums import AgentRole, CodexAuthMode, WorkflowMode

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda match: os.environ.get(match.group(1), match.group(2) or ""), value
        )
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置字典；列表和标量由外部配置整体替换。"""

    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


class AgentProfile(StrictModel):
    prompt_file: Path
    phase_prompt_files: dict[
        Literal["harbor_build", "build_diagnosis", "preflight", "trajectory"],
        Path,
    ] = Field(default_factory=dict)
    model: str
    reasoning_effort: str = "medium"


class CodexSettings(StrictModel):
    auth_mode: CodexAuthMode = CodexAuthMode.API_KEY
    account_home: Path = Path("~/.codex")
    network_proxy: str = ""
    base_url: str = ""
    api_key_env: str = "SAF_CODEX_API_KEY"
    codex_bin: str | None = None
    turn_timeout_s: int = Field(default=3600, ge=30, le=28_800)
    turn_retries: int = Field(default=2, ge=0, le=10)
    retry_backoff_s: int = Field(default=60, ge=1, le=600)
    delivery_repair_attempts: int = Field(
        default=1,
        ge=0,
        le=2,
        description="主交付及附属文件在同一 Agent 会话内允许的修正次数",
    )


class SeedEnvironmentCacheSettings(StrictModel):
    """Seed Docker 环境及其构建期数据的宿主机预热缓存。"""

    mode: Literal["disabled", "prefer", "require"] = "disabled"
    root: Path = Path("var/seed-environment-cache")
    docker_executable: str = Field(default="docker", min_length=1)
    build_docker_host: str | None = Field(
        default=None,
        description=("Seed 预热与 Builder 使用的独立 Docker daemon；空值表示与 rollout 共用"),
    )
    rollout_docker_host: str | None = Field(
        default=None,
        description=(
            "Harbor Rollout 与缓存可用性检查使用的 Docker daemon；空值读取进程 DOCKER_HOST"
        ),
    )
    image_prefix: str = Field(default="saf-seed-environment", min_length=1)
    build_network: Literal["default", "host"] = "default"
    prefer_ipv4: bool = False
    build_concurrency: int = Field(default=2, ge=1, le=8)
    build_timeout_s: int = Field(default=14_400, ge=600, le=86_400)

    @field_validator("image_prefix")
    @classmethod
    def safe_image_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value):
            raise ValueError("image_prefix must be a Docker repository without a tag")
        return value.rstrip("/")

    @field_validator("build_docker_host", "rollout_docker_host")
    @classmethod
    def normalize_docker_host(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class HarborSettings(StrictModel):
    """外部 Harbor 原生配置引用；不解释 agent 或认证协议。"""

    executable: str = Field(default="harbor", min_length=1)
    config_file: Path | None = None
    wall_timeout_s: int = Field(default=86400, ge=30)
    shutdown_grace_s: int = Field(default=30, ge=1, le=300)


class PipelineSettings(StrictModel):
    workflow_mode: WorkflowMode = WorkflowMode.AUDITED
    task_count: int = Field(default=1, ge=1, le=32)
    concurrency: int = Field(default=2, ge=1, le=16)
    required_trajectories: int = Field(default=1, ge=1, le=16)
    max_builder_revisions: int = Field(default=1, ge=0, le=10)
    max_contract_repairs: int = Field(default=1, ge=0, le=3)
    max_distiller_revisions: int = Field(default=1, ge=0, le=3)
    max_solver_attempts: int = Field(default=2, ge=1, le=20)
    builder_mode: Literal["direct", "environment_aware"] = "environment_aware"
    environment_strategy: Literal["synthesize", "reuse_seed"] = Field(
        default="synthesize",
        description=(
            "synthesize 允许 Builder 交付独立环境；reuse_seed 要求新任务复用 Seed 的"
            "Docker 环境与数据，只保留任务特有的小型制品"
        ),
    )
    builder_exploration_soft_timeout_s: int = Field(default=300, ge=0, le=28_800)
    exploration_trials: int = Field(
        default=0,
        ge=0,
        le=8,
        description=("Builder 额外环境探针的最低次数；0 表示由 Builder 根据证据自主决定"),
    )
    difficulty_policy: Literal["preserve"] = "preserve"
    solution_guidance_mode: Literal["strategy", "strategy_seed"] = Field(
        default="strategy",
        description=(
            "Solver 始终接收由当前 Reference 蒸馏的策略卡；strategy_seed 还会从"
            "第一次尝试起读取 SeedBundle 白名单声明的原任务材料与解法"
        ),
    )

    @model_validator(mode="after")
    def feasible_trajectory_budget(self) -> PipelineSettings:
        if self.required_trajectories > self.max_solver_attempts:
            raise ValueError("required_trajectories cannot exceed max_solver_attempts")
        return self


class BatchSettings(StrictModel):
    """固定 Seed 槽位，失败不替换 Seed。"""

    tasks_per_seed: int = Field(default=1, ge=1, le=32)
    generation_concurrency: int = Field(default=2, ge=1, le=16)
    trajectory_concurrency: int = Field(default=2, ge=0, le=16)
    max_generation_resumes: int = Field(default=1, ge=0, le=5)
    generation_resume_backoff_s: float = Field(default=1, ge=0, le=1800)
    environment_poll_s: float = Field(default=2, ge=0.2, le=60)


class FactorySettings(StrictModel):
    storage_root: Path = Path("var/runs")
    prompt_root: Path = Path(str(files("science_agent_factory.resources") / "prompts"))
    allow_host_full_access: bool = False
    codex: CodexSettings = Field(default_factory=CodexSettings)
    harbor: HarborSettings = Field(default_factory=HarborSettings)
    seed_environment_cache: SeedEnvironmentCacheSettings = Field(
        default_factory=SeedEnvironmentCacheSettings
    )
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    batch: BatchSettings = Field(default_factory=BatchSettings)
    agents: dict[AgentRole, AgentProfile]

    @model_validator(mode="after")
    def require_all_roles(self) -> FactorySettings:
        missing = set(AgentRole) - set(self.agents)
        if missing:
            raise ValueError(f"missing Agent profiles: {sorted(item.value for item in missing)}")
        expected_auditor_phases = {
            "build_diagnosis",
            "preflight",
            "trajectory",
        }
        expected_builder_phases = {"harbor_build"}
        for role, profile in self.agents.items():
            phases = set(profile.phase_prompt_files)
            if role not in {AgentRole.AUDITOR, AgentRole.BUILDER} and phases:
                raise ValueError(
                    f"phase_prompt_files are only supported for builder/auditor, got {role.value}"
                )
            if role == AgentRole.AUDITOR and phases and phases != expected_auditor_phases:
                missing_phases = sorted(expected_auditor_phases - phases)
                extra_phases = sorted(phases - expected_auditor_phases)
                raise ValueError(
                    "auditor phase_prompt_files must define build_diagnosis, preflight and trajectory; "
                    f"missing={missing_phases}, extra={extra_phases}"
                )
            if role == AgentRole.BUILDER and phases and phases != expected_builder_phases:
                missing_phases = sorted(expected_builder_phases - phases)
                extra_phases = sorted(phases - expected_builder_phases)
                raise ValueError(
                    "builder phase_prompt_files must define harbor_build; "
                    f"missing={missing_phases}, extra={extra_phases}"
                )
        return self

    @classmethod
    def load(cls, path: str | Path) -> FactorySettings:
        config_path = Path(path).expanduser().resolve()
        override = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(override, dict):
            raise TypeError("configuration root must be a YAML mapping")
        resource_root = files("science_agent_factory.resources")
        defaults = (
            yaml.safe_load((resource_root / "default.yaml").read_text(encoding="utf-8")) or {}
        )
        settings = cls.model_validate(_expand_env(_deep_merge(defaults, override)))
        if "storage_root" in override:
            storage_base = config_path.parent
        else:
            storage_base = Path.cwd()
        if not settings.storage_root.is_absolute():
            settings.storage_root = (storage_base / settings.storage_root).resolve()
        else:
            settings.storage_root = settings.storage_root.expanduser().resolve()
        account_home = settings.codex.account_home.expanduser()
        if not account_home.is_absolute():
            account_home = (config_path.parent / account_home).absolute()
        else:
            account_home = account_home.expanduser().absolute()
        settings.codex = settings.codex.model_copy(update={"account_home": account_home})
        cache = settings.seed_environment_cache
        settings.seed_environment_cache = cache.model_copy(
            update={"root": (config_path.parent / cache.root.expanduser()).resolve()}
        )
        if settings.harbor.config_file is not None:
            settings.harbor.config_file = (
                config_path.parent / settings.harbor.config_file.expanduser()
            ).resolve()
        if "prompt_root" in override:
            prompt_root = settings.prompt_root
            if not prompt_root.is_absolute():
                prompt_root = (config_path.parent / prompt_root).resolve()
            else:
                prompt_root = prompt_root.expanduser().resolve()
            settings.prompt_root = prompt_root
        else:
            settings.prompt_root = Path(str(resource_root / "prompts")).resolve()
        return cls._resolve_prompts(settings)

    @classmethod
    def load_default(cls) -> FactorySettings:
        """读取 wheel 内置安全配置，并把运行目录解析到当前工作目录。"""

        resource_root = files("science_agent_factory.resources")
        raw = yaml.safe_load((resource_root / "default.yaml").read_text(encoding="utf-8")) or {}
        settings = cls.model_validate(_expand_env(raw))
        settings.storage_root = (Path.cwd() / settings.storage_root).resolve()
        account_home = settings.codex.account_home.expanduser()
        if not account_home.is_absolute():
            account_home = (Path.cwd() / account_home).absolute()
        else:
            account_home = account_home.absolute()
        settings.codex = settings.codex.model_copy(update={"account_home": account_home})
        settings.prompt_root = Path(str(resource_root / "prompts")).resolve()
        settings.seed_environment_cache.root = settings.seed_environment_cache.root.resolve()
        return cls._resolve_prompts(settings)

    @classmethod
    def _resolve_prompts(cls, settings: FactorySettings) -> FactorySettings:
        """把角色 Prompt 解析到受限根目录并验证四个审查阶段。"""

        for role, profile in list(settings.agents.items()):
            prompt = profile.prompt_file
            if not prompt.is_absolute():
                prompt = (settings.prompt_root / prompt).resolve()
            else:
                prompt = prompt.expanduser().resolve()
            if not prompt.is_relative_to(settings.prompt_root) or not prompt.is_file():
                raise FileNotFoundError(f"prompt for {role.value} not found: {prompt}")
            phase_prompts: dict[str, Path] = {}
            for phase, configured_prompt in profile.phase_prompt_files.items():
                phase_prompt = configured_prompt
                if not phase_prompt.is_absolute():
                    phase_prompt = (settings.prompt_root / phase_prompt).resolve()
                else:
                    phase_prompt = phase_prompt.expanduser().resolve()
                if (
                    not phase_prompt.is_relative_to(settings.prompt_root)
                    or not phase_prompt.is_file()
                ):
                    raise FileNotFoundError(
                        f"prompt for {role.value}.{phase} not found: {phase_prompt}"
                    )
                phase_prompts[phase] = phase_prompt
            settings.agents[role] = profile.model_copy(
                update={"prompt_file": prompt, "phase_prompt_files": phase_prompts}
            )
        return settings


__all__ = [
    "AgentProfile",
    "BatchSettings",
    "CodexSettings",
    "FactorySettings",
    "HarborSettings",
    "PipelineSettings",
    "SeedEnvironmentCacheSettings",
]
