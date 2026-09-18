"""存储层公共入口。

模块用途：公开文件复制、摘要、路径解析、JSON 写入工具。
主要接口：``copy_artifact``、``reject_symlinks``、``artifact_digest``、``write_json``。
输入：明确的文件系统路径、JSON 值和批次记录。
输出：文件操作结果、摘要、解析路径或写入文件。
副作用与边界：仅操作调用方指定的路径，不调用 Agent 或修改工作流状态。
"""

from .digests import artifact_digest, tree_digest
from .files import copy_artifact, reject_symlinks, remove_path, reset_directory
from .json import write_json
from .paths import resolve_seed_source

__all__ = [
    "artifact_digest",
    "copy_artifact",
    "reject_symlinks",
    "remove_path",
    "reset_directory",
    "resolve_seed_source",
    "tree_digest",
    "write_json",
]
