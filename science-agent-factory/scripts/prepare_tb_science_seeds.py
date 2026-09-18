"""Terminal-Bench Science v0.1.0 到标准 SeedBundle 的边界归一化工具。

模块用途：固定官方 TB Science v0.1.0 提交，递归发现科学任务，并把全部任务或显式选择的轻量
试运行集合转换为核心工作流可读取的 SeedBundle；不负责运行 Agent、Harbor verifier 或批次调度。
主要接口：``prepare_seed_bundles``、``PILOT_TASK_IDS`` 和命令行 ``main``。
输入：固定提交的官方 Git checkout、目标 seed 根目录，以及可选的任务 ID 集合或内置十任务 pilot。
输出：每个入选任务的 ``saf.seed.v2`` 目录和使用根目录相对路径的通用
``saf.seed-import.v1`` 导入清单。
副作用与边界：可执行只读 Git 查询/浅克隆，复制 instruction/environment/solution，并通过受管清单、
同级备份和失败回滚替换目标；明确不复制 tests、verifier、authoring、README 或历史 Agent 轨迹，
原 benchmark 内容只作为私有 seed。
"""

from __future__ import annotations

import argparse
import json
import tomllib
import uuid
from collections.abc import Collection
from pathlib import Path
from typing import Any

from science_agent_factory.contracts.models import SeedBundle, SeedProvenance
from science_agent_factory.storage.files import copy_artifact, remove_path
from science_agent_factory.storage.json import write_json
from science_agent_factory.storage.importing import (
    checkout_pinned_repository,
    replace_managed_import,
    tree_sha256,
)

TB_SCIENCE_VERSION = "v0.1.0"
TB_SCIENCE_COMMIT = "f81afac4f11048e77a15dfc8fb1dbfb897fea0ce"
TB_SCIENCE_REPOSITORY = "https://github.com/harbor-framework/terminal-bench-science.git"

# 五个官方科学大类各两个任务。选择依据是资源声明和本地 seed 体积，而不是降低科学难度。
PILOT_TASK_IDS = (
    "sparse-network-assimilation",
    "masked-spherical-remap",
    "reactor-safety-control",
    "virtual-baseline-localization",
    "mri-harmonization",
    "foraging-cognitive-model",
    "dna-storage-codec",
    "ode-law-discovery",
    "geometric-pharmacophore-alignment",
    "neo-orbit-determination",
)


def _checkout(source: Path) -> Path:
    return checkout_pinned_repository(
        source,
        version=TB_SCIENCE_VERSION,
        repository=TB_SCIENCE_REPOSITORY,
        commit=TB_SCIENCE_COMMIT,
        label="TB Science",
    )


def _task_card(
    task_root: Path,
    tasks_root: Path,
    instruction: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    relative = task_root.relative_to(tasks_root)
    if len(relative.parts) != 3:
        raise ValueError(f"TB Science task must use domain/field/task layout: {relative}")
    domain, field, task_id = relative.parts
    task = metadata.get("task", {})
    details = metadata.get("metadata", {})
    environment = metadata.get("environment", {})
    artifacts = [str(item) for item in metadata.get("artifacts", [])]
    return {
        "id": f"tb-science-v0.1-{task_id}",
        "title": task.get("name") or task_id,
        "instruction": instruction,
        "taskPrompt": instruction,
        "inputs": ["environment/"],
        "outputs": artifacts,
        "category": domain,
        "subcategory": field,
        "domain": details.get("domain", domain),
        "field": details.get("field", field),
        "subfield": details.get("subfield", "unknown"),
        "tags": details.get("tags", task.get("keywords", [])),
        "expert_time_estimate_hours": details.get("expert_time_estimate_hours"),
        "environment": {
            "type": "docker",
            "dockerfile": "environment/Dockerfile",
            "cpus": int(environment.get("cpus", 1)),
            "memory_mb": int(environment.get("memory_mb", 2048)),
            "storage_mb": int(environment.get("storage_mb", 10_240)),
            "gpus": int(environment.get("gpus", 0)),
            "gpu_types": environment.get("gpu_types", []),
            "network_mode": environment.get("network_mode", "public"),
            "build_timeout_sec": environment.get("build_timeout_sec", 600),
        },
    }


def _prepare_one(task_root: Path, tasks_root: Path, destination: Path) -> dict[str, Any]:
    task_id = task_root.name
    task_toml = task_root / "task.toml"
    instruction_path = task_root / "instruction.md"
    environment = task_root / "environment"
    solution = task_root / "solution"
    for required in (task_toml, instruction_path, environment, solution):
        if not required.exists():
            raise FileNotFoundError(f"TB Science seed input is missing: {required}")
    metadata = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    instruction = instruction_path.read_text(encoding="utf-8")
    card = _task_card(task_root, tasks_root, instruction, metadata)
    relative_task = task_root.relative_to(tasks_root).as_posix()
    if card["environment"]["gpus"]:
        return {
            "source_task_id": task_id,
            "source_task_path": relative_task,
            "seed_id": f"tb-science-v0.1-{task_id}",
            "status": "excluded",
            "reason": f"requires GPU types {card['environment']['gpu_types']}",
            "category": card["category"],
            "cpus": card["environment"]["cpus"],
            "memory_mb": card["environment"]["memory_mb"],
        }

    destination.mkdir(parents=True)
    copy_artifact(instruction_path, destination / "instruction.md")
    copy_artifact(task_toml, destination / "source_task.toml")
    copy_artifact(environment, destination / "environment")
    copy_artifact(solution, destination / "solution")
    source_paths = [task_toml, instruction_path, environment, solution]
    license_path = task_root / "LICENSE.md"
    license_label = "Apache-2.0"
    if license_path.is_file():
        copy_artifact(license_path, destination / "SOURCE_LICENSE.md")
        source_paths.append(license_path)
        license_label = "See SOURCE_LICENSE.md"
    write_json(destination / "task_card.json", card)
    seed = SeedBundle(
        schema_version="saf.seed.v2",
        seed_id=f"tb-science-v0.1-{task_id}",
        task_card="task_card.json",
        successful_trajectory=None,
        solution_path="solution",
        environment_dir="environment",
        input_dir=None,
        software_dir="environment",
        provenance=SeedProvenance(
            source="terminal-bench-science",
            source_version=TB_SCIENCE_VERSION,
            source_task_id=task_id,
            source_uri=(
                "https://github.com/harbor-framework/terminal-bench-science/tree/"
                f"{TB_SCIENCE_VERSION}/tasks/{relative_task}"
            ),
            archive_sha256=tree_sha256(source_paths, task_root),
            license=license_label,
        ),
        notes=(
            "Imported as private synthesis seed from the official task, environment, and "
            "solution. Benchmark tests and verifier were not imported; raw benchmark content "
            "must not be exported as training text."
        ),
    )
    write_json(destination / "seed_bundle.json", seed)
    seed.validate_files(destination)
    return {
        "source_task_id": task_id,
        "source_task_path": relative_task,
        "seed_id": seed.seed_id,
        "status": "included",
        "category": card["category"],
        "cpus": card["environment"]["cpus"],
        "memory_mb": card["environment"]["memory_mb"],
        "storage_mb": card["environment"]["storage_mb"],
        "path": destination.name,
    }


def _select_tasks(task_roots: list[Path], task_ids: Collection[str] | None) -> list[Path]:
    by_id: dict[str, Path] = {}
    duplicates: set[str] = set()
    for task_root in task_roots:
        if task_root.name in by_id:
            duplicates.add(task_root.name)
        by_id[task_root.name] = task_root
    if duplicates:
        raise ValueError(f"TB Science task IDs are not unique: {sorted(duplicates)}")
    if task_ids is None:
        return task_roots
    requested = list(dict.fromkeys(task_ids))
    missing = sorted(set(requested) - set(by_id))
    if missing:
        raise ValueError(f"unknown TB Science task IDs: {missing}")
    return [by_id[task_id] for task_id in requested]


def prepare_seed_bundles(
    source: Path,
    destination: Path,
    *,
    task_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    source = _checkout(source.expanduser().resolve())
    destination = destination.expanduser().resolve()
    tasks_root = source / "tasks"
    task_roots = sorted(path.parent for path in tasks_root.rglob("task.toml"))
    if len(task_roots) != 70:
        raise ValueError(f"expected 70 TB Science v0.1.0 tasks, found {len(task_roots)}")
    selected = _select_tasks(task_roots, task_ids)
    staging = destination.with_name(f".{destination.name}.staging-{uuid.uuid4().hex[:8]}")
    staging.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    try:
        for task_root in selected:
            seed_id = f"tb-science-v0.1-{task_root.name}"
            entry = _prepare_one(task_root, tasks_root, staging / seed_id)
            if entry["status"] == "included":
                entry["path"] = entry["seed_id"]
            entries.append(entry)
        manifest = {
            "schema_version": "saf.seed-import.v1",
            "source": "terminal-bench-science",
            "source_version": TB_SCIENCE_VERSION,
            "source_commit": TB_SCIENCE_COMMIT,
            "source_repository": TB_SCIENCE_REPOSITORY,
            "discovered": len(task_roots),
            "selected": len(selected),
            "included": sum(item["status"] == "included" for item in entries),
            "excluded": sum(item["status"] == "excluded" for item in entries),
            "entries": entries,
        }
        write_json(staging / "import_manifest.json", manifest)
        replace_managed_import(
            staging,
            destination,
            source=source,
            source_repository=TB_SCIENCE_REPOSITORY,
            source_version=TB_SCIENCE_VERSION,
        )
        return manifest
    finally:
        if staging.exists():
            remove_path(staging)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="准备固定 TB Science v0.1.0 SeedBundle")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("var/imports/terminal-bench-science/v0.1.0/source"),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("var/seeds/tb-science-v0.1.0"),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--pilot", action="store_true", help="导入内置的跨五领域十任务集合")
    selection.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="只导入指定任务，可重复传入",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    selected = PILOT_TASK_IDS if args.pilot else args.task_ids
    manifest = prepare_seed_bundles(
        args.source,
        args.destination,
        task_ids=selected,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
