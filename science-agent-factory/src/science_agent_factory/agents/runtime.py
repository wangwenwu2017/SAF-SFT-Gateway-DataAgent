"""内部 Builder、Distiller、Auditor 的 Codex SDK 文件交付运行时。

模块用途：内部 Builder、Distiller、Auditor 的 Codex SDK 文件交付运行时。
主要接口：CodexRuntime、AgentExecution、DeliveryContractError、CodexSubscriptionError。
输入：配置、任务制品或状态数据、当前工厂 Python 解释器路径。
输出：交付 Schema（含构建内设计卡）、校验后的数据或明确异常。
副作用与边界：创建和恢复隔离会话、流式保存 SDK 进度并有界中断超时任务；API 模式直连外部 Responses 服务；有限修复交付；清理自身资源。审查和蒸馏仅读取暂存证据，不承担 Solver 或验证执行。
禁用可持久化环境变量的 shell 调试快照；其余会话与进度证据仍按原协议保存。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import shlex
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..contracts.deliveries import (
    HarborBuilderDelivery,
    HarborSelfTestReport,
    HarborTaskMetadata,
    SolverStrategyCard,
)
from ..contracts.design import ExplorationReport, TaskDesignCard
from ..contracts.enums import AgentRole, CodexAuthMode, FailureKind
from ..contracts.environment import EnvironmentRunReport
from ..contracts.reviews import (
    BuilderReflection,
    DistillerReflection,
    SolverReflection,
)
from ..contracts.settings import FactorySettings
from ..storage.files import remove_path
from ..workflow.workspace import AgentWorkspace, ResourceGuard
from .sdk_control import close_sdk, install_queue_wakeup, run_observed_turn

OutputT = TypeVar("OutputT", bound=BaseModel)
DeliveryValidator = Callable[[OutputT], None]

_TRANSIENT_ERROR_MARKERS = (
    "rate limit",
    "too many requests",
    "high demand",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "stream disconnected",
    "response.failed",
)

_INCOMPATIBLE_THREAD_MARKERS = (
    "invalid_encrypted_content",
    "array_above_max_length",
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _strict_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _strict_schema(item) for key, item in value.items()}
    if "properties" in normalized:
        normalized["additionalProperties"] = False
        normalized["required"] = list(normalized.get("properties", {}))
    return normalized


@dataclass(frozen=True, slots=True)
class AgentExecution(Generic[OutputT]):
    output: OutputT
    thread_id: str
    turn_ids: tuple[str, ...]
    final_response: str
    event_path: Path


class DeliveryContractError(RuntimeError):
    """Agent 在有限的同会话修正后仍未交付有效文件。"""

    def __init__(self, message: str, *, thread_id: str | None = None):
        super().__init__(message)
        self.thread_id = thread_id


class CodexSubscriptionError(RuntimeError):
    """订阅认证在启动模型 Thread 前失败，并携带稳定的工作流分类。"""

    def __init__(self, message: str, *, failure_kind: FailureKind, code: str):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.failure_code = code


def _validate_subscription_home(account_home: Path, workspace: Path) -> None:
    """只检查订阅凭据路径和权限，不读取或复制认证内容。"""

    if not account_home.is_absolute() or account_home.is_symlink():
        raise CodexSubscriptionError(
            "Codex subscription account_home must be an absolute non-symlink directory",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )
    # 不仅检查最终目录：父目录中的符号链接也可能把凭据解析到工作区或不受信任位置。
    for parent in (account_home, *account_home.parents):
        if parent == Path(parent.anchor):
            break
        try:
            is_link = parent.is_symlink()
        except OSError as exc:
            raise CodexSubscriptionError(
                "Codex subscription account_home path cannot be inspected",
                failure_kind=FailureKind.POLICY,
                code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
            ) from exc
        if is_link:
            raise CodexSubscriptionError(
                "Codex subscription account_home cannot contain a symlink component",
                failure_kind=FailureKind.POLICY,
                code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
            )
    resolved_home = account_home.resolve(strict=False)
    resolved_workspace = workspace.resolve(strict=False)
    if resolved_home == resolved_workspace or resolved_home.is_relative_to(resolved_workspace):
        raise CodexSubscriptionError(
            "Codex subscription account_home must stay outside the Agent workspace",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )
    try:
        home_stat = account_home.stat()
    except OSError as exc:
        raise CodexSubscriptionError(
            "Codex subscription account_home is unavailable",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        ) from exc
    if not stat.S_ISDIR(home_stat.st_mode) or home_stat.st_uid != os.geteuid():
        raise CodexSubscriptionError(
            "Codex subscription account_home must be an owned directory",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )
    if stat.S_IMODE(home_stat.st_mode) & 0o077:
        raise CodexSubscriptionError(
            "Codex subscription account_home permissions must not grant group/other access",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )
    auth_path = account_home / "auth.json"
    if auth_path.is_symlink():
        raise CodexSubscriptionError(
            "Codex subscription auth.json cannot be a symlink",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )
    try:
        auth_stat = auth_path.stat()
    except OSError as exc:
        raise CodexSubscriptionError(
            "Codex subscription auth.json is missing",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_AUTH_REQUIRED",
        ) from exc
    if not stat.S_ISREG(auth_stat.st_mode) or auth_stat.st_uid != os.geteuid():
        raise CodexSubscriptionError(
            "Codex subscription auth.json must be an owned regular file",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )
    if stat.S_IMODE(auth_stat.st_mode) & 0o077:
        raise CodexSubscriptionError(
            "Codex subscription auth.json permissions must not grant group/other access",
            failure_kind=FailureKind.POLICY,
            code="CODEX_SUBSCRIPTION_CONFIG_INVALID",
        )


class CodexRuntime:
    def __init__(self, settings: FactorySettings):
        if not settings.allow_host_full_access:
            raise PermissionError("正式工作流 requires explicit allow_host_full_access: true")
        self.settings = settings

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
        delivery_validator: DeliveryValidator[OutputT] | None = None,
    ) -> AgentExecution[OutputT]:
        try:
            from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
        except ImportError as exc:
            raise RuntimeError("openai-codex is not installed; run `uv sync`") from exc

        profile = self.settings.agents[role]
        common_instructions = profile.prompt_file.read_text(encoding="utf-8").strip()
        if prompt_phase is None:
            instructions = common_instructions
        else:
            phase_prompt = profile.phase_prompt_files.get(prompt_phase)
            if phase_prompt is None:
                raise ValueError(f"no phase prompt configured for {role.value}.{prompt_phase}")
            phase_instructions = phase_prompt.read_text(encoding="utf-8").strip()
            instructions = f"{phase_instructions}\n\n{common_instructions}"
        workspace = agent_workspace.workspace.resolve()
        target = (workspace / output_relative_path).resolve()
        if not target.is_relative_to(workspace):
            raise PermissionError(f"Agent output escapes workspace: {output_relative_path}")
        if target.exists() or target.is_symlink():
            remove_path(target)

        schema_path = workspace / ".saf" / "schemas" / f"{target.name}.schema.json"
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(
            json.dumps(_strict_schema(output_type.model_json_schema()), indent=2) + "\n",
            encoding="utf-8",
        )
        for name, schema_type in (
            ("environment-run.schema.json", EnvironmentRunReport),
            ("exploration-report.schema.json", ExplorationReport),
            ("builder-reflection.schema.json", BuilderReflection),
            ("task-design-card.schema.json", TaskDesignCard),
            ("distiller-reflection.schema.json", DistillerReflection),
            ("solver-reflection.schema.json", SolverReflection),
            ("solver-strategy-card.schema.json", SolverStrategyCard),
            ("harbor-builder-delivery.schema.json", HarborBuilderDelivery),
            ("harbor-task-metadata.schema.json", HarborTaskMetadata),
            ("harbor-self-test.schema.json", HarborSelfTestReport),
        ):
            (schema_path.parent / name).write_text(
                json.dumps(_strict_schema(schema_type.model_json_schema()), indent=2) + "\n",
                encoding="utf-8",
            )
        env = os.environ.copy()
        subscription = self.settings.codex.auth_mode == CodexAuthMode.CHATGPT_SUBSCRIPTION
        if subscription:
            for name in {
                self.settings.codex.api_key_env,
                "OPENAI_API_KEY",
                "CODEX_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
            }:
                # AsyncCodex 会把 CodexConfig.env 覆盖到它继承的进程环境；显式空值
                # 防止父进程中残留的 API Key 或自定义端点抢占 ChatGPT 登录。
                env[name] = ""
            account_home = self.settings.codex.account_home.expanduser()
            if not account_home.is_absolute():
                account_home = (Path.cwd() / account_home).absolute()
            _validate_subscription_home(account_home, workspace)
            if self.settings.codex.network_proxy:
                for name in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                ):
                    env[name] = self.settings.codex.network_proxy
        else:
            key = env.get(self.settings.codex.api_key_env, "")
            if key:
                env["OPENAI_API_KEY"] = key
                env["CODEX_API_KEY"] = key
        env["SAF_RUN_ID"] = agent_workspace.run_id
        env["SAF_RESOURCE_PREFIX"] = agent_workspace.resource_prefix
        builder_docker_host = (
            self.settings.seed_environment_cache.build_docker_host
            if role == AgentRole.BUILDER
            else None
        )
        if builder_docker_host is not None:
            env["DOCKER_HOST"] = builder_docker_host
        state_dir = agent_workspace.state_dir or (agent_workspace.run_dir / "codex-state")
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_dir, 0o700)
        env["CODEX_HOME"] = str(account_home if subscription else state_dir)
        event_path = agent_workspace.event_path
        event_path.parent.mkdir(parents=True, exist_ok=True)
        provider_base_url = "" if subscription else self.settings.codex.base_url
        overrides = [f'model_reasoning_effort="{profile.reasoning_effort}"', "features.shell_snapshot=false"]
        model_provider = "openai" if subscription else None
        if subscription:
            overrides.extend(
                (
                    'forced_login_method="chatgpt"',
                    'model_provider="openai"',
                    f"sqlite_home={json.dumps(str(state_dir))}",
                )
            )
        if provider_base_url:
            model_provider = "saf_api"
            overrides.extend(
                [
                    'model_provider="saf_api"',
                    'model_providers.saf_api.name="saf_api"',
                    f"model_providers.saf_api.base_url={json.dumps(provider_base_url.rstrip('/'))}",
                    f"model_providers.saf_api.env_key={json.dumps(self.settings.codex.api_key_env)}",
                    'model_providers.saf_api.wire_api="responses"',
                ]
            )
        sdk_config = CodexConfig(
            codex_bin=self.settings.codex.codex_bin,
            config_overrides=tuple(overrides),
            cwd=str(workspace),
            env=env,
        )
        turn_ids: list[str] = []
        active_thread_id = thread_id
        final_response = ""
        guard = ResourceGuard(
            agent_workspace.resource_prefix,
            docker_host=builder_docker_host,
        )

        codex = AsyncCodex(sdk_config)
        install_queue_wakeup(codex)
        codex_entered = False
        try:
            await codex.__aenter__()
            codex_entered = True
            if subscription:
                try:
                    # 0.147.0 的 account(read) 在 refresh_token=True 时会触发刷新流程，
                    # 该流程可能先返回 account=null；读取已登录快照应使用 False。
                    account_response = await asyncio.wait_for(
                        codex.account(refresh_token=False), timeout=30
                    )
                except Exception as exc:
                    raise CodexSubscriptionError(
                        "Codex could not validate the ChatGPT subscription account",
                        failure_kind=FailureKind.INFRASTRUCTURE,
                        code="CODEX_SUBSCRIPTION_NETWORK_UNAVAILABLE",
                    ) from exc
                account = (
                    account_response.account.root if account_response.account is not None else None
                )
                if (
                    account_response.requires_openai_auth is not True
                    or getattr(account, "type", None) != "chatgpt"
                ):
                    raise CodexSubscriptionError(
                        "Codex is not signed in with ChatGPT in configured account_home",
                        failure_kind=FailureKind.POLICY,
                        code="CODEX_SUBSCRIPTION_AUTH_REQUIRED",
                    )
            thread = None
            if thread_id:
                try:
                    thread = await codex.thread_resume(
                        thread_id,
                        approval_mode=ApprovalMode.deny_all,
                        base_instructions=instructions,
                        cwd=str(workspace),
                        model=profile.model,
                        model_provider=model_provider,
                        sandbox=Sandbox.full_access,
                    )
                except Exception as exc:  # noqa: BLE001 - SDK 没有稳定的恢复异常基类
                    self._append_event(
                        event_path,
                        {
                            "type": "thread_resume_failed",
                            "thread_id": thread_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
            if thread is None:
                thread = await codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=instructions,
                    cwd=str(workspace),
                    ephemeral=not persistent,
                    model=profile.model,
                    model_provider=model_provider,
                    sandbox=Sandbox.full_access,
                )
                resumed_existing_thread = False
            else:
                resumed_existing_thread = True
            active_thread_id = thread.id
            thread_restart_count = 0
            max_thread_restarts = max(1, self.settings.codex.turn_retries)
            read_only_role = role != AgentRole.BUILDER
            environment_contract = (
                "- 本阶段只读取暂存证据并提交报告，不得启动 Docker、QEMU、普通服务或长任务，"
                "也不需要 environment_report.json。\n"
                if read_only_role
                else (
                    "- 你可以自主检查环境并直接启动 Docker、QEMU 或普通进程。\n"
                    f"- 所有资源名称、QEMU -name、pidfile 和临时 overlay 必须使用前缀 "
                    f"`{agent_workspace.resource_prefix}`。\n"
                    "- 结束前必须停止环境并在 environment_report.json 中登记实际命令和清理结果。\n"
                )
            )
            boundary_contract = (
                "- 本阶段只在当前工作区内读写 `inputs/` 与 `outputs/`；不得使用绝对路径"
                "访问工作区之外的仓库源码、其他运行记录或宿主文件，也不要搜索或安装"
                "工厂源码。结果由工厂在你提交后自行校验，无需导入工厂解释器或 Pydantic。\n"
                if read_only_role
                else (
                    f"- 工厂校验专用 Python：{shlex.quote(sys.executable)}。"
                    "导入 science_agent_factory、Pydantic 或计算任务指纹时使用此解释器；"
                    "不要使用宿主默认 python/python3，不需要搜索或安装工厂源码。"
                    "此路径仅用于宿主交付校验，不能写入正式任务文件。\n"
                )
            )
            contract = (
                "\n\n正式交付协议：\n"
                f"- 结果 JSON 必须写入 `{target.relative_to(workspace).as_posix()}`。\n"
                f"- Schema 位于 `{schema_path.relative_to(workspace).as_posix()}`。\n"
                "- 共享环境、探索和任务包 Schema 位于 `.saf/schemas/`。\n"
                + boundary_contract
                + environment_contract
                + "- 最终回复不能替代正式文件。\n"
            )
            validation_error: Exception | None = None
            try:
                repair_budget = self.settings.codex.delivery_repair_attempts
                for validation_attempt in range(repair_budget + 1):
                    turn_message = (
                        (message + contract)
                        if validation_attempt == 0
                        else (
                            "正式结果或其附属交付文件缺失，或者未通过契约校验。"
                            f"错误：{type(validation_error).__name__}: {validation_error}\n"
                            "请只针对这个明确错误检查 `.saf/schemas/`，修正主结果及相关附属文件；"
                            "如果证据表明确实未通过，应修正决策而不是伪造通过结果。"
                            f"最后重写 `{target.relative_to(workspace).as_posix()}`，并确认环境资源已经清理。"
                        )
                    )
                    while True:
                        try:
                            result = await self._run_turn(thread, turn_message, role, event_path)
                            break
                        except Exception as exc:
                            incompatible_marker = next(
                                (
                                    marker
                                    for marker in _INCOMPATIBLE_THREAD_MARKERS
                                    if marker in str(exc).lower()
                                ),
                                None,
                            )
                            if (
                                incompatible_marker is None
                                or thread_restart_count >= max_thread_restarts
                            ):
                                raise
                            previous_thread_id = thread.id
                            previous_thread_was_resumed = resumed_existing_thread
                            cleaned = await asyncio.to_thread(guard.cleanup_owned)
                            leftovers = await asyncio.to_thread(guard.leftovers)
                            if cleaned or leftovers:
                                self._append_event(
                                    event_path,
                                    {
                                        "type": "resource_cleanup_before_thread_restart",
                                        "prefix": agent_workspace.resource_prefix,
                                        "cleaned": cleaned,
                                        "leftovers": leftovers,
                                    },
                                )
                            if leftovers:
                                raise RuntimeError(
                                    "Agent left owned environment resources before thread restart: "
                                    f"{leftovers}"
                                )
                            thread = await codex.thread_start(
                                approval_mode=ApprovalMode.deny_all,
                                base_instructions=instructions,
                                cwd=str(workspace),
                                ephemeral=not persistent,
                                model=profile.model,
                                model_provider=model_provider,
                                sandbox=Sandbox.full_access,
                            )
                            active_thread_id = thread.id
                            thread_restart_count += 1
                            resumed_existing_thread = False
                            self._append_event(
                                event_path,
                                {
                                    "type": "thread_restarted",
                                    "role": role.value,
                                    "previous_thread_id": previous_thread_id,
                                    "thread_id": active_thread_id,
                                    "reason": incompatible_marker,
                                    "previous_thread_was_resumed": (previous_thread_was_resumed),
                                    "restart_attempt": thread_restart_count,
                                    "max_restart_attempts": max_thread_restarts,
                                },
                            )
                    turn_ids.append(result.id)
                    final_response = result.final_response or ""
                    try:
                        if target.is_symlink() or not target.is_file():
                            raise FileNotFoundError(target)
                        output = output_type.model_validate_json(target.read_text(encoding="utf-8"))
                        if delivery_validator is not None:
                            delivery_validator(output)
                        break
                    except (OSError, UnicodeError, ValidationError, ValueError) as exc:
                        validation_error = exc
                        self._append_event(
                            event_path,
                            {
                                "type": "delivery_validation_failed",
                                "role": role.value,
                                "validation_attempt": validation_attempt + 1,
                                "repair_budget": repair_budget,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        )
                else:
                    raise DeliveryContractError(
                        f"Agent output failed validation after repair: {validation_error}",
                        thread_id=active_thread_id,
                    )
            finally:
                cleaned = await asyncio.to_thread(guard.cleanup_owned)
                leftovers = await asyncio.to_thread(guard.leftovers)
                if cleaned or leftovers:
                    self._append_event(
                        event_path,
                        {
                            "type": "resource_cleanup",
                            "prefix": agent_workspace.resource_prefix,
                            "cleaned": cleaned,
                            "leftovers": leftovers,
                        },
                    )
                if leftovers:
                    raise RuntimeError(f"Agent left owned environment resources: {leftovers}")
        finally:
            if codex_entered:
                prior_error = sys.exc_info()[0] is not None
                try:
                    await close_sdk(codex)
                except Exception as exc:
                    self._append_event(event_path, {
                        "type": "sdk_close_failed", "error_type": type(exc).__name__,
                    })
                    if not prior_error:
                        raise

        assert active_thread_id is not None
        return AgentExecution(
            output=output,
            thread_id=active_thread_id,
            turn_ids=tuple(turn_ids),
            final_response=final_response,
            event_path=event_path,
        )

    async def _run_turn(self, thread, message: str, role: AgentRole, event_path: Path):
        for attempt in range(self.settings.codex.turn_retries + 1):
            try:
                def emit(payload):
                    self._append_event(event_path, {"role": role.value, **payload})
                    if payload["type"] in {"heartbeat", "turn_started", "exploration_budget_reminder"}:
                        print(json.dumps({"role": role.value, **payload}, ensure_ascii=False), flush=True)

                result = await run_observed_turn(
                    thread, message,
                    timeout_s=self.settings.codex.turn_timeout_s,
                    exploration_s=(self.settings.pipeline.builder_exploration_soft_timeout_s
                                   if role == AgentRole.BUILDER else 0),
                    emit=emit,
                )
                self._append_event(
                    event_path,
                    {
                        "type": "turn",
                        "role": role.value,
                        "thread_id": thread.id,
                        "turn_id": result.id,
                        "status": str(result.status),
                        "duration_ms": result.duration_ms,
                        "usage": _jsonable(result.usage),
                        "items": [_jsonable(item) for item in result.items],
                        "final_response": result.final_response,
                    },
                )
                return result
            except TimeoutError as exc:
                self._append_event(
                    event_path,
                    {
                        "type": "turn_timeout",
                        "role": role.value,
                        "thread_id": thread.id,
                        "timeout_s": self.settings.codex.turn_timeout_s,
                    },
                )
                raise TimeoutError("Codex turn exceeded configured timeout") from exc
            except Exception as exc:
                message_lower = str(exc).lower()
                retryable = any(marker in message_lower for marker in _TRANSIENT_ERROR_MARKERS)
                if not retryable or attempt >= self.settings.codex.turn_retries:
                    raise
                delay = self.settings.codex.retry_backoff_s * (attempt + 1)
                self._append_event(
                    event_path,
                    {
                        "type": "turn_retry",
                        "role": role.value,
                        "attempt": attempt + 1,
                        "delay_s": delay,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _append_event(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                                     **payload}, ensure_ascii=False, default=str) + "\n")
