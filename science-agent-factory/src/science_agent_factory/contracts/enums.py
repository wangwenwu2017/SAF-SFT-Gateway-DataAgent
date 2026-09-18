"""工作流和持久状态使用的有限枚举。

模块用途：设计内化于构建，无独立设计阶段；集中定义 Agent 角色、工作流模式、制品格式、阶段和失败状态，避免配置、制品合同与状态模型重复声明枚举。
主要接口：``AgentRole``、``WorkflowMode``、``CodexAuthMode``、``CandidateStage``、``FailureKind``、``JobStatus``、``BatchStatus``。
输入：无；枚举值来自配置、Agent 交付和持久 JSON。
输出：可序列化的 ``StrEnum`` 成员。
副作用与边界：只提供类型定义，不读取文件、不修改状态、不启动外部进程。
"""

from enum import StrEnum


class AgentRole(StrEnum):
    BUILDER = "builder"
    DISTILLER = "distiller"
    AUDITOR = "auditor"


class WorkflowMode(StrEnum):
    """同一状态机支持的工作流强度。"""

    LIGHTWEIGHT = "lightweight"
    AUDITED = "audited"


class CodexAuthMode(StrEnum):
    """Codex 访问 OpenAI 模型时使用的计费与认证入口。"""

    API_KEY = "api_key"
    CHATGPT_SUBSCRIPTION = "chatgpt_subscription"


class CandidateStage(StrEnum):
    PENDING = "pending"
    BUILT = "built"
    PREFLIGHT_ACCEPTED = "preflight_accepted"
    SOLVED = "solved"
    CURATED = "curated"
    FAILED = "failed"


class FailureKind(StrEnum):
    """工作流失败的稳定分类，供 Job 恢复和批次调度共同使用。"""

    SEMANTIC = "semantic"
    CONTRACT = "contract"
    INFRASTRUCTURE = "infrastructure"
    POLICY = "policy"
    INTERNAL = "internal"


class JobStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    TASKS_READY = "tasks_ready"
    CURATED = "curated"
    FAILED = "failed"


class BatchStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CURATED = "curated"
    BLOCKED = "blocked"
    FAILED = "failed"
