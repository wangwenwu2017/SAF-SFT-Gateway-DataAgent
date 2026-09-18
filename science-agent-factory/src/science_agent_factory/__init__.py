"""Science Agent Factory 正式公共入口。

模块用途：提供版本信息，并延迟公开单 Job 工厂、批次控制器和正式配置，避免普通导入启动 SDK。
主要接口：``__version__``、``ScienceAgentFactory``、``BatchController`` 和 ``FactorySettings``。
输入：Python 包元数据以及调用方访问的公共属性。
输出：版本字符串和稳定的工作流/配置类型。
副作用与边界：只在访问工作流类型时导入具体实现；不会创建 Job、调用模型或启动环境。
"""

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .config import FactorySettings

__all__ = ["BatchController", "FactorySettings", "ScienceAgentFactory", "__version__"]

try:
    __version__ = version("science-agent-factory")
except PackageNotFoundError:  # Source tree imported before installation.
    __version__ = "0+unknown"


def __getattr__(name: str) -> Any:
    if name == "ScienceAgentFactory":
        from .workflow.factory import ScienceAgentFactory

        return ScienceAgentFactory
    if name == "BatchController":
        from .workflow.batch import BatchController

        return BatchController
    raise AttributeError(name)
