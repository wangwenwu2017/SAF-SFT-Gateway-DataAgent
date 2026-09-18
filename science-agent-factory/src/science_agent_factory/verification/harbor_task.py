"""完整 Harbor Task 与 Builder 旁路元数据的确定性验证。

模块用途：在不把 Harbor Python 包加入核心依赖的前提下，验证极简 Builder 直接交付的 Harbor 1.4
目录、源码可移植性、自测索引和 Seed 环境来源；静态拒绝 verifier 读取隐藏 solution，且对
Dockerfile 的 COPY/ADD（含反斜杠续行）仅拒绝顶层 solution、tests 或父目录来源；不运行模型、Docker
或 Harbor 命令。
主要接口：``validate_harbor_task``、``validate_harbor_metadata``、
validate_harbor_delivery、harbor_task_fingerprint 和 HarborTaskInfo。
输入：Harbor Task 根目录、包含 design_card.json 的旁路 metadata 目录、期望的任务/Seed 标识、环境策略和 SeedBundle 根目录。
输出：解析后的任务身份与超时、结构化 metadata，或带明确原因的路径/契约异常。
副作用与边界：只读取文件、权限位和目录树；拒绝符号链接、凭据文件、本机专用镜像与宿主绝对挂载，
但不替代 Builder 已记录的 Oracle/Nop 真实运行自测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..contracts.deliveries import HarborSelfTestReport, HarborTaskMetadata
from ..contracts.design import ExplorationReport, TaskDesignCard
from ..contracts.environment import EnvironmentRunReport
from ..contracts.models import SeedBundle
from ..storage.digests import tree_digest
from ..storage.files import reject_symlinks

_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOST_PATH_MARKERS = ("/data/hy/", "/home/hy/", "inputs/seed", "outputs/bundle")
_FORBIDDEN_VERIFIER_PATH = re.compile(r"(?i)(?:/app/solution|/solution|solution/solve\.sh)")
_STANDARD_REWARD_FILE = re.compile(r"(?i)(?:reward\.txt|rewards\.json)")


@dataclass(frozen=True, slots=True)
class HarborTaskInfo:
    """经过验证的 Harbor Task 最小运行信息。"""

    task_id: str
    title: str
    timeout_s: int


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a TOML/YAML mapping")
    return value


def _require_executable(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if not os.access(path, os.X_OK):
        raise ValueError(f"Harbor script must be executable: {path.name}")


def _validate_compose_portability(path: Path) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services = _mapping(payload, "docker-compose root").get("services", {})
    for service_name, raw_service in _mapping(services, "docker-compose services").items():
        service = _mapping(raw_service, f"service {service_name}")
        if service.get("pull_policy") == "never":
            raise ValueError("portable Harbor task cannot require pull_policy=never")
        for volume in service.get("volumes", []) or []:
            source: str | None = None
            if isinstance(volume, str) and ":" in volume:
                source = volume.split(":", 1)[0]
            elif isinstance(volume, dict) and volume.get("type") == "bind":
                source = str(volume.get("source", ""))
            if source and (Path(source).is_absolute() or source.startswith("~")):
                raise ValueError("portable Harbor task cannot use an absolute host bind mount")


def _is_forbidden_dockerfile_source(source: str) -> bool:
    """判断 COPY/ADD 来源是否越界或读取任务解答/测试目录。"""

    path = Path(source.replace("\\", "/"))
    parts = path.parts
    if ".." in parts:
        return True
    while parts and parts[0] == ".":
        parts = parts[1:]
    return bool(parts and parts[0] in {"solution", "tests"})


def _dockerfile_copy_sources(line: str) -> list[str]:
    """解析一行 COPY/ADD，返回来源参数（不含目标和选项）。"""

    try:
        tokens = shlex.split(line, comments=True, posix=True)
    except ValueError as exc:
        raise ValueError("Harbor Dockerfile contains malformed COPY/ADD syntax") from exc
    if len(tokens) < 3:
        return []
    arguments = [token for token in tokens[1:] if not token.startswith("--")]
    return arguments[:-1]


def _dockerfile_logical_lines(text: str) -> list[str]:
    """合并 Dockerfile 反斜杠续行，保留每条逻辑指令供校验。"""

    logical_lines: list[str] = []
    pending = ""
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        logical_lines.append(pending + line.lstrip())
        pending = ""
    if pending:
        logical_lines.append(pending)
    return logical_lines


def _validate_environment_portability(environment: Path) -> None:
    forbidden_names = {"auth.json", ".env"}
    for path in environment.rglob("*"):
        if path.name in forbidden_names:
            raise ValueError(f"Harbor environment contains a credential file: {path.name}")
    dockerfile = environment / "Dockerfile"
    compose_files = [
        path
        for path in (environment / "docker-compose.yaml", environment / "compose.yaml")
        if path.is_file()
    ]
    if not dockerfile.is_file() and not compose_files:
        raise FileNotFoundError("Harbor environment requires Dockerfile or Compose")
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        for line in _dockerfile_logical_lines(text):
            stripped = line.strip()
            if re.match(r"(?i)^(copy|add)\s+", stripped):
                sources = _dockerfile_copy_sources(stripped)
            else:
                sources = []
            if any(_is_forbidden_dockerfile_source(source) for source in sources):
                raise ValueError(
                    "Harbor environment cannot copy parent, solution, or tests content"
                )
        if any(marker in text for marker in _HOST_PATH_MARKERS):
            raise ValueError("Harbor Dockerfile contains a machine-specific host path")
    for compose in compose_files:
        if any(
            marker in compose.read_text(encoding="utf-8", errors="replace")
            for marker in _HOST_PATH_MARKERS
        ):
            raise ValueError("Harbor Compose contains a machine-specific host path")
        _validate_compose_portability(compose)


def validate_harbor_task(
    task_dir: Path,
    *,
    expected_task_id: str | None = None,
) -> HarborTaskInfo:
    """验证完整、可移植且启用 verification 的 Harbor 1.4 任务。"""

    root = task_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    reject_symlinks(root)
    instruction = root / "instruction.md"
    config_path = root / "task.toml"
    environment = root / "environment"
    solution = root / "solution" / "solve.sh"
    verifier = root / "tests" / "test.sh"
    if not instruction.is_file() or not instruction.read_text(encoding="utf-8").strip():
        raise FileNotFoundError("Harbor instruction.md is missing or empty")
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    if not environment.is_dir():
        raise FileNotFoundError(environment)
    _require_executable(solution)
    _require_executable(verifier)
    verifier_text = verifier.read_text(encoding="utf-8", errors="replace")
    if _FORBIDDEN_VERIFIER_PATH.search(verifier_text):
        raise ValueError(
            "Harbor verifier cannot reference /solution, /app/solution, or solution/solve.sh"
        )
    if not (
        any(
            marker in verifier_text
            for marker in (
                "/logs/verifier/reward.txt",
                "/logs/verifier/rewards.json",
            )
        )
        or ("/logs/verifier" in verifier_text and _STANDARD_REWARD_FILE.search(verifier_text))
    ):
        raise ValueError("Harbor test.sh must write a standard verifier reward file")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if str(config.get("schema_version")) != "1.4":
        raise ValueError("Harbor task.toml must use schema_version 1.4")
    task = _mapping(config.get("task"), "task.toml [task]")
    package_name = str(task.get("name", ""))
    if not _PACKAGE_NAME.fullmatch(package_name) or ".." in package_name:
        raise ValueError("Harbor task.name must use a safe org/name package identifier")
    task_id = package_name.split("/", 1)[1]
    if expected_task_id is not None and task_id != expected_task_id:
        raise ValueError("Harbor task.name must end with the isolated candidate ID")
    agent = _mapping(config.get("agent", {}), "task.toml [agent]")
    try:
        timeout_s = int(float(agent.get("timeout_sec", 0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("Harbor agent.timeout_sec must be numeric") from exc
    if timeout_s < 30:
        raise ValueError("Harbor agent.timeout_sec must be at least 30 seconds")
    environment_config = _mapping(config.get("environment", {}), "task.toml [environment]")
    if environment_config.get("docker_image") is not None:
        raise ValueError("portable Harbor task cannot depend on docker_image")
    if any(marker in config_path.read_text(encoding="utf-8") for marker in _HOST_PATH_MARKERS):
        raise ValueError("Harbor task.toml contains a machine-specific host path")
    _validate_environment_portability(environment)
    return HarborTaskInfo(
        task_id=task_id,
        title=str(task.get("description") or task_id),
        timeout_s=timeout_s,
    )


def _resolve_metadata_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError(f"metadata file is missing: {relative}")
    return path


def _validate_evidence(root: Path, paths: list[str]) -> None:
    for relative in paths:
        _resolve_metadata_file(root, relative)


def validate_harbor_metadata(
    metadata_dir: Path,
    *,
    expected_task_id: str,
    expected_seed_id: str,
    environment_strategy: str,
    seed_dir: Path,
) -> HarborTaskMetadata:
    """验证 Harbor 旁路元数据、自测证据以及复用 Seed 的来源摘要。"""

    root = metadata_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    reject_symlinks(root)
    metadata = HarborTaskMetadata.model_validate_json(
        _resolve_metadata_file(root, "metadata.json").read_text(encoding="utf-8")
    )
    if metadata.task_id != expected_task_id or metadata.seed_id != expected_seed_id:
        raise ValueError("Harbor metadata task/Seed identity mismatch")
    design = TaskDesignCard.model_validate_json(
        _resolve_metadata_file(root, "design_card.json").read_text(encoding="utf-8")
    )
    if design.task_id != expected_task_id or design.seed_id != expected_seed_id:
        raise ValueError("Harbor design card task/Seed identity mismatch")
    expected_source = "seed_copy" if environment_strategy == "reuse_seed" else "synthesized"
    if metadata.environment_source != expected_source:
        raise ValueError("Harbor metadata environment source differs from configuration")
    if expected_source == "seed_copy":
        seed = SeedBundle.model_validate_json(
            (seed_dir / "seed_bundle.json").read_text(encoding="utf-8")
        )
        if seed.environment_dir is None:
            raise ValueError("reuse_seed requires a Seed environment")
        actual_digest = tree_digest(seed_dir / seed.environment_dir)
        if metadata.seed_environment_sha256 != actual_digest:
            raise ValueError("Harbor metadata Seed environment digest mismatch")

    exploration = ExplorationReport.model_validate_json(
        _resolve_metadata_file(root, metadata.exploration_report_path).read_text(encoding="utf-8")
    )
    if exploration.seed_id != expected_seed_id:
        raise ValueError("Harbor exploration report belongs to another Seed")
    experiments = [exploration.seed_reproduction, *exploration.experiments]
    for experiment in experiments:
        _validate_evidence(root, experiment.evidence_paths)

    environment_report = EnvironmentRunReport.model_validate_json(
        _resolve_metadata_file(root, metadata.environment_report_path).read_text(encoding="utf-8")
    )
    if not environment_report.cleaned:
        raise ValueError("Harbor Builder environment report is not cleaned")
    _validate_evidence(root, environment_report.evidence_paths)

    self_test = HarborSelfTestReport.model_validate_json(
        _resolve_metadata_file(root, metadata.self_test_report_path).read_text(encoding="utf-8")
    )
    _validate_evidence(root, self_test.evidence_paths)
    return metadata


def harbor_task_fingerprint(task_dir: Path) -> str:
    """对 Harbor 正式任务内容和脚本执行位计算稳定 SHA-256。"""

    root = task_dir.resolve()
    reject_symlinks(root)
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0x" if os.access(path, os.X_OK) else b"\0-")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "HarborTaskInfo",
    "harbor_task_fingerprint",
    "validate_harbor_delivery",
    "validate_harbor_metadata",
    "validate_harbor_task",
]


def validate_harbor_delivery(
    task_dir: Path,
    metadata_dir: Path,
    *,
    expected_task_id: str,
    expected_seed_id: str,
    environment_strategy: str,
    seed_dir: Path,
) -> tuple[HarborTaskInfo, HarborTaskMetadata]:
    """聚合 Harbor Task、旁路 metadata 和 Seed 摘要校验。

    Builder 交付经常同时缺少多个文件或字段；一次性返回全部独立错误，
    让同一 Thread 能在一次修复中完成结构纠正，避免按错误顺序反复消耗模型调用。
    """

    errors: list[str] = []
    task_info: HarborTaskInfo | None = None
    metadata: HarborTaskMetadata | None = None
    try:
        task_info = validate_harbor_task(
            task_dir,
            expected_task_id=expected_task_id,
        )
    except (FileNotFoundError, PermissionError, TypeError, ValueError) as exc:
        errors.append(f"task: {exc}")
    try:
        metadata = validate_harbor_metadata(
            metadata_dir,
            expected_task_id=expected_task_id,
            expected_seed_id=expected_seed_id,
            environment_strategy=environment_strategy,
            seed_dir=seed_dir,
        )
    except (FileNotFoundError, PermissionError, TypeError, ValueError) as exc:
        errors.append(f"metadata: {exc}")
    if metadata is not None and metadata.environment_source == "seed_copy":
        try:
            actual_digest = tree_digest(task_dir.resolve() / "environment")
            if actual_digest != metadata.seed_environment_sha256:
                raise ValueError("Harbor task environment is not an exact Seed source copy")
        except (FileNotFoundError, PermissionError, TypeError, ValueError) as exc:
            errors.append(f"environment: {exc}")
    if metadata is not None:
        try:
            validate_self_test_evidence(task_dir, metadata_dir, metadata.self_test_report_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"self-test: {exc}")
    if errors:
        raise ValueError("Harbor delivery validation failed:\n- " + "\n- ".join(errors))
    assert task_info is not None and metadata is not None
    return task_info, metadata


def validate_self_test_evidence(task_dir: Path, metadata_dir: Path, report_path: str) -> None:
    """核对真实 Harbor Oracle/负例结果和最终任务摘要。"""
    report = HarborSelfTestReport.model_validate_json(
        _resolve_metadata_file(metadata_dir, report_path).read_text()
    )
    if report.task_sha256 != harbor_task_fingerprint(task_dir):
        raise ValueError("self-test evidence belongs to another task revision")
    for relative, expected, agent in [
        (report.oracle_result_path, 1, "oracle"),
        (report.negative_result_path, 0, "nop"),
    ]:
        path = _resolve_metadata_file(metadata_dir, relative)
        payload = json.loads(path.read_text())
        if (
            not isinstance(payload, dict)
            or not payload.get("trial_name")
            or not payload.get("finished_at")
            or payload.get("exception_info")
        ):
            raise ValueError("self-test requires a completed Harbor trial without exceptions")
        info = payload.get("agent_info")
        verifier = payload.get("verifier_result")
        if (
            not isinstance(info, dict)
            or not isinstance(verifier, dict)
            or not isinstance(verifier.get("rewards"), dict)
        ):
            raise TypeError("self-test requires structured agent and verifier evidence")
        if info.get("name") != agent:
            raise ValueError("self-test requires Harbor oracle and nop result evidence")
        reward = verifier["rewards"].get("reward")
        if type(reward) not in {int, float} or reward != expected:
            raise ValueError("self-test Harbor reward does not match required outcome")
