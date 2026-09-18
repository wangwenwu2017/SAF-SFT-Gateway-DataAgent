"""Codex 运行时的 full-access、持久 Thread、有限重试与文件协议单元测试。

测试目的：确认 API Key 模式保持原有 full-access、持久 Thread、有限重试和外部 API 直连；ChatGPT 订阅模式
则复用安全的账户目录、隔离 SQLite、清除 API Key、忽略自定义 API 配置，并在启动 Thread 前拒绝无效账户。
被测接口：``CodexRuntime.run_to_file``、``CodexSubscriptionError``。
输入与夹具：临时 Prompt/工作区、受限权限的假账户目录、SDK 0.147.0 ``GetAccountResponse``、Fake
AsyncCodex/Thread 和 HarborBuilderDelivery Schema。
预期结果：两种认证入口不会静默互相回退；订阅预检成功时强制 ChatGPT/OpenAI provider，失败时保留稳定
错误分类且不启动 Thread；构建内设计卡 Schema 可用，设计卡缺失进入同线程修复；Builder 恢复、有限交付修正与 API 网关行为正确；交付协议提供工厂解释器，避免误用宿主 Python。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import openai_codex
import pytest
from openai_codex.generated.v2_all import GetAccountResponse

from science_agent_factory.agents.runtime import (
    CodexRuntime,
    CodexSubscriptionError,
    DeliveryContractError,
)
from science_agent_factory.contracts.deliveries import HarborBuilderDelivery
from science_agent_factory.contracts.enums import AgentRole, CodexAuthMode, FailureKind
from science_agent_factory.contracts.settings import (
    AgentProfile,
    CodexSettings,
    FactorySettings,
    SeedEnvironmentCacheSettings,
)
from science_agent_factory.workflow.workspace import AgentWorkspace, ResourceGuard


class FakeThread:
    messages: ClassVar[list[str]] = []

    def __init__(self, thread_id: str, target: Path):
        self.id = thread_id
        self.target = target
        self.turns = 0

    async def run(self, message: str):
        self.messages.append(message)
        self.turns += 1
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text(
            HarborBuilderDelivery(task_id="candidate-001", summary="ok").model_dump_json(),
            encoding="utf-8",
        )
        return SimpleNamespace(
            id=f"turn-{self.turns}",
            status="completed",
            duration_ms=1,
            usage={},
            items=[],
            final_response="file written",
        )


class RetryOnceThread(FakeThread):
    async def run(self, message: str):
        if self.turns == 0:
            self.turns += 1
            raise RuntimeError(
                "We're currently experiencing high demand, which may cause temporary errors."
            )
        return await super().run(message)


class IncompatibleResumeThread(FakeThread):
    error_code: ClassVar[str] = "invalid_encrypted_content"

    async def run(self, message: str):
        raise RuntimeError(
            f'{{"error":{{"code":"{self.error_code}",'
            '"message":"encrypted content could not be verified"}}'
        )


class FakeAsyncCodex:
    target: Path
    starts: ClassVar[list[dict]] = []
    resumes: ClassVar[list[tuple[str, dict]]] = []
    configs: ClassVar[list] = []

    def __init__(self, config):
        self.config = config
        self.configs.append(config)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        return FakeThread("thread-builder", self.target)

    async def thread_resume(self, thread_id: str, **kwargs):
        self.resumes.append((thread_id, kwargs))
        return FakeThread(thread_id, self.target)


class IncompatibleResumeAsyncCodex(FakeAsyncCodex):
    async def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        return FakeThread("thread-replacement", self.target)

    async def thread_resume(self, thread_id: str, **kwargs):
        self.resumes.append((thread_id, kwargs))
        return IncompatibleResumeThread(thread_id, self.target)


class IncompatibleFreshAsyncCodex(FakeAsyncCodex):
    """首个新 Thread 失效，替代 Thread 可正常交付。"""

    async def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        if len(self.starts) == 1:
            return IncompatibleResumeThread("thread-broken", self.target)
        return FakeThread("thread-replacement", self.target)


class RepeatedIncompatibleFreshAsyncCodex(FakeAsyncCodex):
    """前两个新 Thread 失效，第二个替代 Thread 可正常交付。"""

    async def thread_start(self, **kwargs):
        self.starts.append(kwargs)
        if len(self.starts) <= 2:
            return IncompatibleResumeThread(f"thread-broken-{len(self.starts)}", self.target)
        return FakeThread("thread-replacement-2", self.target)


class SubscriptionAsyncCodex(FakeAsyncCodex):
    """返回 SDK 0.147.0 真实响应类型的订阅预检替身。"""

    account_response: ClassVar[GetAccountResponse]
    account_responses: ClassVar[dict[bool, GetAccountResponse] | None] = None
    account_error: ClassVar[Exception | None] = None
    account_refreshes: ClassVar[list[bool]] = []

    async def account(self, *, refresh_token: bool = False):
        self.account_refreshes.append(refresh_token)
        if self.account_error is not None:
            raise self.account_error
        if self.account_responses is not None:
            return self.account_responses[refresh_token]
        return self.account_response


def _settings(tmp_path: Path) -> FactorySettings:
    prompt = tmp_path / "builder.md"
    prompt.write_text("构建并交付文件。\n", encoding="utf-8")
    profile = AgentProfile(prompt_file=prompt, model="fake", reasoning_effort="medium")
    return FactorySettings(
        storage_root=tmp_path / "var",
        prompt_root=tmp_path,
        allow_host_full_access=True,
        agents={role: profile for role in AgentRole},
    )


def _subscription_home(tmp_path: Path, *, with_auth: bool = True) -> Path:
    account_home = tmp_path / "subscription-account"
    account_home.mkdir(mode=0o700)
    account_home.chmod(0o700)
    if with_auth:
        auth = account_home / "auth.json"
        auth.write_text('{"test": true}\n', encoding="utf-8")
        auth.chmod(0o600)
    return account_home


@pytest.mark.asyncio
async def test_builder_receives_isolated_build_docker_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "builder-workspace"
    target = workspace / "outputs" / "builder_result.json"
    workspace.mkdir()
    FakeAsyncCodex.target = target
    FakeAsyncCodex.starts = []
    FakeAsyncCodex.configs = []
    FakeThread.messages = []
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    monkeypatch.setattr("sys.executable", "/worker/factory env/bin/python")
    settings = _settings(tmp_path)
    settings.seed_environment_cache = SeedEnvironmentCacheSettings(
        build_docker_host="unix:///run/build.sock"
    )

    await CodexRuntime(settings).run_to_file(
        AgentRole.BUILDER,
        AgentWorkspace(
            run_id="builder-dual-docker-12345678",
            resource_prefix="saf-builder-dual-docker-12345678",
            run_dir=tmp_path / "builder-run",
            workspace=workspace,
            event_path=tmp_path / "builder-run" / "events.jsonl",
        ),
        "构建任务。",
        HarborBuilderDelivery,
        "outputs/builder_result.json",
        persistent=True,
    )

    assert FakeAsyncCodex.configs[-1].env["DOCKER_HOST"] == ("unix:///run/build.sock")
    assert "'/worker/factory env/bin/python'" in FakeThread.messages[0]
    assert "science_agent_factory" in FakeThread.messages[0]


@pytest.mark.asyncio
async def test_codex_retries_transient_high_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr("science_agent_factory.agents.runtime.asyncio.sleep", no_wait)
    workspace = tmp_path / "retry-workspace"
    target = workspace / "outputs" / "build_result.json"
    target.parent.mkdir(parents=True)
    settings = _settings(tmp_path).model_copy(
        update={
            "codex": CodexSettings(
                turn_retries=1,
                retry_backoff_s=1,
            )
        }
    )
    event_path = tmp_path / "retry-events.jsonl"

    result = await CodexRuntime(settings)._run_turn(
        RetryOnceThread("thread-retry", target),
        "build",
        AgentRole.BUILDER,
        event_path,
    )

    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert result.status == "completed"
    assert [event["type"] for event in events] == ["turn_retry", "turn"]
    assert events[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_codex_uses_full_access_and_resumes_builder_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    FakeAsyncCodex.target = target
    FakeAsyncCodex.starts = []
    FakeAsyncCodex.resumes = []
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    agent_workspace = AgentWorkspace(
        run_id="builder-candidate-001-00-12345678",
        resource_prefix="saf-builder-candidate-001-00-12345678",
        run_dir=tmp_path / "run",
        workspace=workspace,
        event_path=tmp_path / "run" / "events.jsonl",
    )
    runtime = CodexRuntime(_settings(tmp_path))

    first = await runtime.run_to_file(
        AgentRole.BUILDER,
        agent_workspace,
        "build",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        persistent=True,
    )
    second = await runtime.run_to_file(
        AgentRole.BUILDER,
        agent_workspace,
        "revise",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        thread_id=first.thread_id,
        persistent=True,
    )

    assert first.output.task_id == "candidate-001"
    assert second.thread_id == "thread-builder"
    assert FakeAsyncCodex.starts[0]["ephemeral"] is False
    assert FakeAsyncCodex.starts[0]["sandbox"] == openai_codex.Sandbox.full_access
    assert FakeAsyncCodex.starts[0]["approval_mode"] == openai_codex.ApprovalMode.deny_all
    assert FakeAsyncCodex.resumes[0][0] == "thread-builder"
    assert (workspace / ".saf/schemas/exploration-report.schema.json").is_file()
    assert (workspace / ".saf/schemas/task-design-card.schema.json").is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "ok"


@pytest.mark.asyncio
async def test_codex_places_auditor_phase_prompt_before_common_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    FakeAsyncCodex.target = target
    FakeAsyncCodex.starts = []
    FakeAsyncCodex.resumes = []
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    settings = _settings(tmp_path)
    common = tmp_path / "auditor-common.md"
    common.write_text("公共审查规则。\n", encoding="utf-8")
    phase_prompts = {}
    for phase in ("build_diagnosis", "preflight", "trajectory"):
        prompt = tmp_path / f"auditor-{phase}.md"
        prompt.write_text(f"阶段规则：{phase}。\n", encoding="utf-8")
        phase_prompts[phase] = prompt
    settings.agents[AgentRole.AUDITOR] = AgentProfile(
        prompt_file=common,
        phase_prompt_files=phase_prompts,
        model="fake",
        reasoning_effort="medium",
    )

    await CodexRuntime(settings).run_to_file(
        AgentRole.AUDITOR,
        AgentWorkspace(
            run_id="auditor-preflight-001-00-12345678",
            resource_prefix="saf-auditor-preflight-001-00-12345678",
            run_dir=tmp_path / "run",
            workspace=workspace,
            event_path=tmp_path / "run" / "events.jsonl",
        ),
        "review",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        prompt_phase="preflight",
    )

    assert FakeAsyncCodex.starts[-1]["base_instructions"] == (
        "阶段规则：preflight。\n\n公共审查规则。"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ["invalid_encrypted_content", "array_above_max_length"],
)
async def test_codex_restarts_thread_when_encrypted_context_is_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    IncompatibleResumeAsyncCodex.target = target
    IncompatibleResumeAsyncCodex.starts = []
    IncompatibleResumeAsyncCodex.resumes = []
    IncompatibleResumeThread.error_code = error_code
    monkeypatch.setattr(openai_codex, "AsyncCodex", IncompatibleResumeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    event_path = tmp_path / "run" / "events.jsonl"

    execution = await CodexRuntime(_settings(tmp_path)).run_to_file(
        AgentRole.BUILDER,
        AgentWorkspace(
            run_id="builder-restart-001-01-12345678",
            resource_prefix="saf-builder-restart-001-01-12345678",
            run_dir=tmp_path / "run",
            workspace=workspace,
            event_path=event_path,
        ),
        "revise from staged previous bundle and review",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        thread_id="thread-old",
        persistent=True,
    )

    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert execution.thread_id == "thread-replacement"
    assert execution.output.task_id == "candidate-001"
    assert IncompatibleResumeAsyncCodex.resumes[0][0] == "thread-old"
    assert len(IncompatibleResumeAsyncCodex.starts) == 1
    assert [event["type"] for event in events] == ["thread_restarted", "turn"]
    assert events[0]["previous_thread_id"] == "thread-old"
    assert events[0]["reason"] == error_code


@pytest.mark.asyncio
async def test_codex_restarts_fresh_thread_when_encrypted_context_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    IncompatibleFreshAsyncCodex.target = target
    IncompatibleFreshAsyncCodex.starts = []
    IncompatibleFreshAsyncCodex.resumes = []
    IncompatibleResumeThread.error_code = "invalid_encrypted_content"
    monkeypatch.setattr(openai_codex, "AsyncCodex", IncompatibleFreshAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    event_path = tmp_path / "run" / "events.jsonl"

    execution = await CodexRuntime(_settings(tmp_path)).run_to_file(
        AgentRole.BUILDER,
        AgentWorkspace(
            run_id="builder-fresh-restart-001-00-12345678",
            resource_prefix="saf-builder-fresh-restart-001-00-12345678",
            run_dir=tmp_path / "run",
            workspace=workspace,
            event_path=event_path,
        ),
        "build from staged files",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        persistent=True,
    )

    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert execution.thread_id == "thread-replacement"
    assert len(IncompatibleFreshAsyncCodex.starts) == 2
    assert IncompatibleFreshAsyncCodex.resumes == []
    assert [event["type"] for event in events] == ["thread_restarted", "turn"]
    assert events[0]["previous_thread_id"] == "thread-broken"
    assert events[0]["reason"] == "invalid_encrypted_content"


@pytest.mark.asyncio
async def test_codex_restarts_replacement_thread_within_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    RepeatedIncompatibleFreshAsyncCodex.target = target
    RepeatedIncompatibleFreshAsyncCodex.starts = []
    RepeatedIncompatibleFreshAsyncCodex.resumes = []
    IncompatibleResumeThread.error_code = "invalid_encrypted_content"
    monkeypatch.setattr(openai_codex, "AsyncCodex", RepeatedIncompatibleFreshAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    event_path = tmp_path / "run" / "events.jsonl"

    execution = await CodexRuntime(_settings(tmp_path)).run_to_file(
        AgentRole.BUILDER,
        AgentWorkspace(
            run_id="builder-repeated-restart-001-00-12345678",
            resource_prefix="saf-builder-repeated-restart-001-00-12345678",
            run_dir=tmp_path / "run",
            workspace=workspace,
            event_path=event_path,
        ),
        "build from staged files",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        persistent=True,
    )

    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    restart_events = [event for event in events if event["type"] == "thread_restarted"]
    assert execution.thread_id == "thread-replacement-2"
    assert len(RepeatedIncompatibleFreshAsyncCodex.starts) == 3
    assert [event["restart_attempt"] for event in restart_events] == [1, 2]
    assert all(event["max_restart_attempts"] == 2 for event in restart_events)


@pytest.mark.asyncio
async def test_delivery_validator_repairs_once_inside_same_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    FakeAsyncCodex.target = target
    FakeAsyncCodex.starts = []
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    calls = 0

    def validate_delivery(_delivery: HarborBuilderDelivery) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("verifier execution results do not match declared scenarios")

    execution = await CodexRuntime(_settings(tmp_path)).run_to_file(
        AgentRole.BUILDER,
        AgentWorkspace(
            run_id="builder-repair-001-00-12345678",
            resource_prefix="saf-builder-repair-001-00-12345678",
            run_dir=tmp_path / "run-repair",
            workspace=workspace,
            event_path=tmp_path / "run-repair" / "events.jsonl",
        ),
        "build",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        delivery_validator=validate_delivery,
    )

    events = [
        json.loads(line) for line in execution.event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert calls == 2
    assert len(execution.turn_ids) == 2
    assert sum(item["type"] == "delivery_validation_failed" for item in events) == 1


@pytest.mark.asyncio
async def test_delivery_validator_stops_after_bounded_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    FakeAsyncCodex.target = target
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    event_path = tmp_path / "run-bounded" / "events.jsonl"

    def reject_delivery(_delivery: HarborBuilderDelivery) -> None:
        raise ValueError("still invalid")

    with pytest.raises(DeliveryContractError, match="failed validation after repair"):
        await CodexRuntime(_settings(tmp_path)).run_to_file(
            AgentRole.BUILDER,
            AgentWorkspace(
                run_id="builder-bounded-001-00-12345678",
                resource_prefix="saf-builder-bounded-001-00-12345678",
                run_dir=tmp_path / "run-bounded",
                workspace=workspace,
                event_path=event_path,
            ),
            "build",
            HarborBuilderDelivery,
            "outputs/build_result.json",
            delivery_validator=reject_delivery,
        )

    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert sum(item["type"] == "delivery_validation_failed" for item in events) == 2


@pytest.mark.asyncio
async def test_codex_routes_directly_to_external_responses_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    FakeAsyncCodex.target = target
    FakeAsyncCodex.configs = []
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    settings = _settings(tmp_path).model_copy(
        update={
            "codex": CodexSettings(
                base_url="http://127.0.0.1:1/v1",
            )
        }
    )
    agent_workspace = AgentWorkspace(
        run_id="builder-capture-001-00-12345678",
        resource_prefix="saf-builder-capture-001-00-12345678",
        run_dir=tmp_path / "run-capture",
        workspace=workspace,
        event_path=tmp_path / "run-capture" / "events.jsonl",
    )

    execution = await CodexRuntime(settings).run_to_file(
        AgentRole.BUILDER,
        agent_workspace,
        "build",
        HarborBuilderDelivery,
        "outputs/build_result.json",
    )

    assert not hasattr(execution, "gateway_trace_dir")
    assert not (agent_workspace.run_dir / "gateway").exists()
    overrides = FakeAsyncCodex.configs[-1].config_overrides
    assert 'model_providers.saf_api.base_url="http://127.0.0.1:1/v1"' in overrides
    assert 'model_providers.saf_api.wire_api="responses"' in overrides
    assert 'model_providers.saf_api.env_key="SAF_CODEX_API_KEY"' in overrides


@pytest.mark.asyncio
async def test_subscription_uses_account_home_without_api_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "subscription-workspace"
    target = workspace / "outputs" / "build_result.json"
    workspace.mkdir()
    account_home = _subscription_home(tmp_path)
    monkeypatch.chdir(tmp_path)
    original_auth = (account_home / "auth.json").read_bytes()
    SubscriptionAsyncCodex.target = target
    SubscriptionAsyncCodex.starts = []
    SubscriptionAsyncCodex.configs = []
    SubscriptionAsyncCodex.account_refreshes = []
    # SDK 0.147.0：refresh_token=True 可能返回空账户，False 才返回已登录快照。
    SubscriptionAsyncCodex.account_responses = {
        True: GetAccountResponse.model_validate({"account": None, "requiresOpenaiAuth": True}),
        False: GetAccountResponse.model_validate(
            {
                "account": {
                    "type": "chatgpt",
                    "email": "subscriber@example.test",
                    "planType": "plus",
                },
                "requiresOpenaiAuth": True,
            }
        ),
    }
    SubscriptionAsyncCodex.account_error = None
    SubscriptionAsyncCodex.account_response = GetAccountResponse.model_validate(
        {
            "account": {
                "type": "chatgpt",
                "email": "subscriber@example.test",
                "planType": "plus",
            },
            "requiresOpenaiAuth": True,
        }
    )
    monkeypatch.setattr(openai_codex, "AsyncCodex", SubscriptionAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])
    monkeypatch.setenv("SAF_CODEX_API_KEY", "metered-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("CODEX_API_KEY", "codex-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://inherited.example/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy-inherited.example/v1")

    settings = _settings(tmp_path)
    settings.codex = CodexSettings(
        auth_mode=CodexAuthMode.CHATGPT_SUBSCRIPTION,
        # 直接构造 Settings 时路径仍可能是相对路径；运行时应与 load() 一样规范化。
        account_home=Path("subscription-account"),
        network_proxy="http://127.0.0.1:17897",
        base_url="https://metered-gateway.example/v1",
    )
    agent_workspace = AgentWorkspace(
        run_id="builder-subscription-001-12345678",
        resource_prefix="saf-builder-subscription-001-12345678",
        run_dir=tmp_path / "subscription-run",
        workspace=workspace,
        event_path=tmp_path / "subscription-run" / "events.jsonl",
    )

    await CodexRuntime(settings).run_to_file(
        AgentRole.BUILDER,
        agent_workspace,
        "build",
        HarborBuilderDelivery,
        "outputs/build_result.json",
        persistent=True,
    )

    config = SubscriptionAsyncCodex.configs[-1]
    assert SubscriptionAsyncCodex.account_refreshes == [False]
    assert config.env["CODEX_HOME"] == str(account_home.resolve())
    assert config.env["SAF_CODEX_API_KEY"] == ""
    assert config.env["OPENAI_API_KEY"] == ""
    assert config.env["CODEX_API_KEY"] == ""
    assert config.env["OPENAI_BASE_URL"] == ""
    assert config.env["OPENAI_API_BASE"] == ""
    assert config.env["HTTP_PROXY"] == "http://127.0.0.1:17897"
    assert config.env["https_proxy"] == "http://127.0.0.1:17897"
    assert 'forced_login_method="chatgpt"' in config.config_overrides
    assert 'model_provider="openai"' in config.config_overrides
    assert f'sqlite_home="{agent_workspace.run_dir / "codex-state"}"' in config.config_overrides
    assert not any("saf_api" in item for item in config.config_overrides)
    assert SubscriptionAsyncCodex.starts[0]["model_provider"] == "openai"
    assert not (agent_workspace.run_dir / "gateway").exists()
    assert (account_home / "auth.json").read_bytes() == original_auth


@pytest.mark.asyncio
async def test_subscription_requires_secure_auth_file_before_sdk_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "missing-auth-workspace"
    workspace.mkdir()
    account_home = _subscription_home(tmp_path, with_auth=False)
    SubscriptionAsyncCodex.configs = []
    SubscriptionAsyncCodex.account_responses = None
    monkeypatch.setattr(openai_codex, "AsyncCodex", SubscriptionAsyncCodex)
    settings = _settings(tmp_path)
    settings.codex = CodexSettings(
        auth_mode=CodexAuthMode.CHATGPT_SUBSCRIPTION,
        account_home=account_home,
    )

    with pytest.raises(CodexSubscriptionError) as raised:
        await CodexRuntime(settings).run_to_file(
            AgentRole.BUILDER,
            AgentWorkspace(
                run_id="builder-missing-auth-001-12345678",
                resource_prefix="saf-builder-missing-auth-001-12345678",
                run_dir=tmp_path / "missing-auth-run",
                workspace=workspace,
                event_path=tmp_path / "missing-auth-run" / "events.jsonl",
            ),
            "build",
            HarborBuilderDelivery,
            "outputs/build_result.json",
        )

    assert raised.value.failure_kind == FailureKind.POLICY
    assert raised.value.failure_code == "CODEX_SUBSCRIPTION_AUTH_REQUIRED"
    assert SubscriptionAsyncCodex.configs == []


@pytest.mark.asyncio
async def test_subscription_rejects_symlinked_account_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """订阅凭据目录的父路径含符号链接时也必须在 SDK 启动前拒绝。"""

    workspace = tmp_path / "symlink-workspace"
    workspace.mkdir()
    real_home = _subscription_home(tmp_path)
    link_parent = tmp_path / "linked-parent"
    link_parent.symlink_to(tmp_path, target_is_directory=True)
    linked_home = link_parent / real_home.name
    monkeypatch.setattr(openai_codex, "AsyncCodex", SubscriptionAsyncCodex)
    settings = _settings(tmp_path)
    settings.codex = CodexSettings(
        auth_mode=CodexAuthMode.CHATGPT_SUBSCRIPTION,
        account_home=linked_home,
    )

    with pytest.raises(CodexSubscriptionError) as raised:
        await CodexRuntime(settings).run_to_file(
            AgentRole.BUILDER,
            AgentWorkspace(
                run_id="builder-symlink-auth-001-12345678",
                resource_prefix="saf-builder-symlink-auth-001-12345678",
                run_dir=tmp_path / "symlink-run",
                workspace=workspace,
                event_path=tmp_path / "symlink-run" / "events.jsonl",
            ),
            "build",
            HarborBuilderDelivery,
            "outputs/build_result.json",
        )

    assert raised.value.failure_kind == FailureKind.POLICY
    assert raised.value.failure_code == "CODEX_SUBSCRIPTION_CONFIG_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_payload",
    [
        {"account": None, "requiresOpenaiAuth": True},
        {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True},
        {
            "account": {
                "type": "chatgpt",
                "email": "subscriber@example.test",
                "planType": "plus",
            },
            "requiresOpenaiAuth": False,
        },
    ],
    ids=("signed-out", "api-key-account", "provider-does-not-require-openai-auth"),
)
async def test_subscription_rejects_non_chatgpt_account_before_thread_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_payload: dict,
) -> None:
    workspace = tmp_path / "wrong-account-workspace"
    workspace.mkdir()
    account_home = _subscription_home(tmp_path)
    SubscriptionAsyncCodex.target = workspace / "outputs" / "build_result.json"
    SubscriptionAsyncCodex.starts = []
    SubscriptionAsyncCodex.configs = []
    SubscriptionAsyncCodex.account_refreshes = []
    SubscriptionAsyncCodex.account_responses = None
    SubscriptionAsyncCodex.account_error = None
    SubscriptionAsyncCodex.account_response = GetAccountResponse.model_validate(account_payload)
    monkeypatch.setattr(openai_codex, "AsyncCodex", SubscriptionAsyncCodex)
    settings = _settings(tmp_path)
    settings.codex = CodexSettings(
        auth_mode=CodexAuthMode.CHATGPT_SUBSCRIPTION,
        account_home=account_home,
    )

    with pytest.raises(CodexSubscriptionError) as raised:
        await CodexRuntime(settings).run_to_file(
            AgentRole.BUILDER,
            AgentWorkspace(
                run_id="builder-wrong-account-001-12345678",
                resource_prefix="saf-builder-wrong-account-001-12345678",
                run_dir=tmp_path / "wrong-account-run",
                workspace=workspace,
                event_path=tmp_path / "wrong-account-run" / "events.jsonl",
            ),
            "build",
            HarborBuilderDelivery,
            "outputs/build_result.json",
        )

    assert raised.value.failure_kind == FailureKind.POLICY
    assert raised.value.failure_code == "CODEX_SUBSCRIPTION_AUTH_REQUIRED"
    assert SubscriptionAsyncCodex.account_refreshes == [False]
    assert SubscriptionAsyncCodex.starts == []


@pytest.mark.asyncio
async def test_subscription_maps_account_preflight_failure_without_starting_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "account-network-workspace"
    workspace.mkdir()
    account_home = _subscription_home(tmp_path)
    SubscriptionAsyncCodex.target = workspace / "outputs" / "build_result.json"
    SubscriptionAsyncCodex.starts = []
    SubscriptionAsyncCodex.account_refreshes = []
    SubscriptionAsyncCodex.account_responses = None
    SubscriptionAsyncCodex.account_error = RuntimeError("transport unavailable")
    monkeypatch.setattr(openai_codex, "AsyncCodex", SubscriptionAsyncCodex)
    settings = _settings(tmp_path)
    settings.codex = CodexSettings(
        auth_mode=CodexAuthMode.CHATGPT_SUBSCRIPTION,
        account_home=account_home,
    )

    with pytest.raises(CodexSubscriptionError) as raised:
        await CodexRuntime(settings).run_to_file(
            AgentRole.BUILDER,
            AgentWorkspace(
                run_id="builder-account-network-001-12345678",
                resource_prefix="saf-builder-account-network-001-12345678",
                run_dir=tmp_path / "account-network-run",
                workspace=workspace,
                event_path=tmp_path / "account-network-run" / "events.jsonl",
            ),
            "build",
            HarborBuilderDelivery,
            "outputs/build_result.json",
        )

    SubscriptionAsyncCodex.account_error = None
    assert raised.value.failure_kind == FailureKind.INFRASTRUCTURE
    assert raised.value.failure_code == "CODEX_SUBSCRIPTION_NETWORK_UNAVAILABLE"
    assert SubscriptionAsyncCodex.account_refreshes == [False]
    assert SubscriptionAsyncCodex.starts == []


@pytest.mark.asyncio
async def test_missing_design_card_is_repaired_in_builder_thread(tmp_path, monkeypatch):
    from science_agent_factory.verification.harbor_task import validate_harbor_delivery
    from tests.unit.test_harbor_task import _harbor_metadata, _harbor_task

    workspace = tmp_path / "workspace"
    output = workspace / "outputs"
    task = _harbor_task(output)
    metadata = _harbor_metadata(output)
    card = metadata / "design_card.json"
    valid_card = card.read_text()
    card.unlink()
    target = output / "build_result.json"
    FakeAsyncCodex.target = target
    FakeAsyncCodex.starts = []
    FakeThread.messages = []
    original_run = FakeThread.run

    async def repair_on_second_turn(self, message):
        if self.turns:
            card.write_text(valid_card)
        return await original_run(self, message)

    monkeypatch.setattr(FakeThread, "run", repair_on_second_turn)
    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(ResourceGuard, "cleanup_owned", lambda self: [])
    monkeypatch.setattr(ResourceGuard, "leftovers", lambda self: [])

    def validate(delivery):
        validate_harbor_delivery(
            task, metadata,
            expected_task_id=delivery.task_id,
            expected_seed_id="seed-001",
            environment_strategy="synthesize",
            seed_dir=tmp_path / "unused-seed",
        )

    execution = await CodexRuntime(_settings(tmp_path)).run_to_file(
        AgentRole.BUILDER,
        AgentWorkspace(
            run_id="builder-card-12345678",
            resource_prefix="saf-builder-card-12345678",
            run_dir=tmp_path / "run",
            workspace=workspace,
            event_path=tmp_path / "run/events.jsonl",
        ),
        "build", HarborBuilderDelivery, "outputs/build_result.json",
        persistent=True, delivery_validator=validate,
    )
    assert len(execution.turn_ids) == 2
    assert len(FakeAsyncCodex.starts) == 1
    assert "design_card.json" in FakeThread.messages[1]
    assert card.is_file()
