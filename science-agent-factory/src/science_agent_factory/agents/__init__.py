"""内部 Codex 角色与外部 Harbor 执行边界。

模块用途：内部 Codex 角色与外部 Harbor 执行边界。
主要接口：CodexRuntime、HarborRunner、TaskBuilderAgent、DistillerAgent、AuditorAgent。
输入：配置、任务制品或状态数据。
输出：校验后的数据或明确异常。
副作用与边界：导入不启动模型或外部进程。
"""

from .harbor import HarborRunner
from .roles import AuditorAgent, DistillerAgent, TaskBuilderAgent
from .runtime import CodexRuntime

__all__ = ["AuditorAgent", "CodexRuntime", "DistillerAgent", "HarborRunner", "TaskBuilderAgent"]
